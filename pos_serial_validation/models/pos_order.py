# -*- coding: utf-8 -*-
from collections import defaultdict

from odoo import api, models, _
from odoo.exceptions import UserError


class PosOrder(models.Model):
    _inherit = 'pos.order'

    def _process_order(self, order, existing_order):
        """Validate serial/lot and stock constraints before processing the order."""
        self._validate_order_serials(order)
        return super()._process_order(order, existing_order)

    @api.model
    def _get_pos_config_from_order(self, order_vals):
        session_id = order_vals.get('session_id')
        if not session_id:
            return False
        session = self.env['pos.session'].browse(session_id)
        return session.config_id if session.exists() else False

    @api.model
    def _extract_command_values(self, command):
        if isinstance(command, (list, tuple)) and len(command) == 3:
            return command[2] or {}
        if isinstance(command, dict):
            return command
        return {}

    @api.model
    def _iter_lot_values(self, pack_lot_ids):
        for lot_cmd in pack_lot_ids or []:
            vals = self._extract_command_values(lot_cmd)
            if vals:
                yield vals

    @api.model
    def _get_lot_available_qty_in_pos_location(self, lot, pos_config, pos_location=False):
        if not lot or not pos_config:
            return 0.0

        pos_location = pos_location or self.env['product.template']._get_pos_source_location(pos_config)
        if not pos_location:
            return 0.0

        domain = [
            ('lot_id', '=', lot.id),
            ('location_id', 'child_of', pos_location.id),
            ('location_id.usage', '=', 'internal'),
        ]
        company_ids = [False]
        if pos_config.company_id:
            company_ids.append(pos_config.company_id.id)
        if pos_location.company_id:
            company_ids.append(pos_location.company_id.id)
        company_ids = list(dict.fromkeys(company_ids))
        if len(company_ids) > 1:
            domain.append(('company_id', 'in', company_ids))

        quant_env = self.env['stock.quant'].sudo().with_company(pos_config.company_id or self.env.company)
        quants = quant_env.search(domain)
        return sum(quants.mapped('quantity')) - sum(quants.mapped('reserved_quantity'))

    @api.model
    def _find_order_lot(self, lot_name, product_id, pos_config):
        StockLot = self.env['stock.lot']
        pos_location = self.env['product.template']._get_pos_source_location(pos_config) if pos_config else False

        if isinstance(lot_name, int):
            lot = StockLot.browse(lot_name).exists()
            if lot and lot.product_id.id == product_id:
                return lot, lot.name
            return False, lot_name

        serial_name = str(lot_name)
        domain = [
            ('name', '=', serial_name),
            ('product_id', '=', product_id),
        ]
        company_ids = [False]
        if pos_config and pos_config.company_id:
            company_ids.append(pos_config.company_id.id)
        if pos_location and pos_location.company_id:
            company_ids.append(pos_location.company_id.id)
        company_ids = list(dict.fromkeys(company_ids))
        if len(company_ids) > 1:
            domain.append(('company_id', 'in', company_ids))

        lot = StockLot.search(domain, limit=1)
        return lot, serial_name

    @api.model
    def _validate_order_serials(self, order_vals):
        lines = order_vals.get('lines', [])
        seen_serials = set()

        pos_config = self._get_pos_config_from_order(order_vals)
        pos_location = self.env['product.template']._get_pos_source_location(pos_config) if pos_config else False

        requested_qty_by_product = defaultdict(float)
        tracked_line_items = []

        for line_cmd in lines:
            line_vals = self._extract_command_values(line_cmd)
            if not line_vals:
                continue

            product_id = line_vals.get('product_id')
            qty = float(line_vals.get('qty', 0) or 0)
            pack_lot_ids = line_vals.get('pack_lot_ids', [])
            is_refund_line = bool(line_vals.get('refunded_orderline_id'))

            if not product_id or qty <= 0 or is_refund_line:
                continue

            product = self.env['product.product'].browse(product_id).exists()
            if not product:
                continue

            if product.tracking not in ('serial', 'lot'):
                if product.type not in ('product', 'consu'):
                    continue
                requested_qty_by_product[product.id] += qty
                continue

            tracked_line_items.append((product, qty, pack_lot_ids))

        if requested_qty_by_product and pos_config and pos_location:
            available_qty_by_product = self.env['product.template']._get_pos_available_qty_by_product_ids(
                list(requested_qty_by_product.keys()),
                pos_config,
                pos_location,
            )
            for product_id, requested_qty in requested_qty_by_product.items():
                available_qty = available_qty_by_product.get(product_id, 0.0)
                if available_qty < requested_qty:
                    product = self.env['product.product'].browse(product_id)
                    raise UserError(_(
                        'The available quantity does not cover the requested amount for product "%s".'
                    ) % product.display_name)

        for product, qty, pack_lot_ids in tracked_line_items:
            serial_count = 0

            for lot_vals in self._iter_lot_values(pack_lot_ids):
                lot_name = lot_vals.get('lot_name') or lot_vals.get('lot_id')
                if not lot_name:
                    continue

                serial_key = (str(lot_name), product.id)
                if serial_key in seen_serials:
                    raise UserError(_(
                        'Serial/Lot "%s" is duplicated in the same order.'
                    ) % lot_name)
                seen_serials.add(serial_key)

                lot, serial_name = self._find_order_lot(lot_name, product.id, pos_config)
                if not lot:
                    raise UserError(_(
                        'Serial/Lot "%s" was not found in stock.'
                    ) % serial_name)

                if lot.serial_status == 'sold' or (
                    product.tracking == 'serial' and lot.product_qty != 1
                ):
                    raise UserError(_(
                        'Serial/Lot "%s" is already sold and has not been returned.\n'
                        'A sold serial/lot cannot be sold again.'
                    ) % serial_name)

                if pos_config and pos_location:
                    available_qty = self._get_lot_available_qty_in_pos_location(
                        lot,
                        pos_config,
                        pos_location=pos_location,
                    )
                    if available_qty <= 0:
                        raise UserError(_(
                            'Serial/Lot "%s" is not available in the POS source location.'
                        ) % serial_name)

                serial_count += 1

            if product.tracking == 'serial' and serial_count < int(qty):
                raise UserError(_(
                    'Please provide all required serial numbers for product "%s".'
                ) % product.display_name)


class PosOrderLine(models.Model):
    _inherit = 'pos.order.line'

    def _is_return_line(self):
        """Return True when line quantity is negative (return line)."""
        return self.qty < 0

    def action_mark_serials_sold(self):
        """Mark serial/lot lines as sold after payment."""
        for line in self.filtered(lambda l: not l._is_return_line()):
            for pack_lot in line.pack_lot_ids:
                lot = self.env['stock.lot'].search([
                    ('name', '=', pack_lot.lot_name),
                    ('product_id', '=', line.product_id.id),
                    ('company_id', '=', self.env.company.id),
                ], limit=1)
                if lot:
                    lot.action_mark_sold(order_line=line)

    def action_mark_serials_returned(self):
        """
        Mark serial/lot lines as returned.
        Called when confirming a POS return order.
        """
        for line in self.filtered(lambda l: l._is_return_line()):
            for pack_lot in line.pack_lot_ids:
                lot = self.env['stock.lot'].search([
                    ('name', '=', pack_lot.lot_name),
                    ('product_id', '=', line.product_id.id),
                    ('company_id', '=', self.env.company.id),
                ], limit=1)
                if lot and lot.serial_status == 'sold':
                    lot.action_mark_returned(order_line=line)


class PosOrderWithReturn(models.Model):
    _inherit = 'pos.order'

    def action_pos_order_paid(self):
        """Update serial/lot state after payment (sale or return)."""
        res = super().action_pos_order_paid()
        for order in self:
            order.lines.action_mark_serials_sold()
            order.lines.action_mark_serials_returned()
        return res

# -*- coding: utf-8 -*-
import math

from odoo import _, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_compare


class ProductProduct(models.Model):
    _inherit = 'product.product'

    def _lock_inventory_adjustment(self, company):
        """Serialize controlled adjustments, including cost-only changes."""
        self.ensure_one()
        if company not in self.env.user.company_ids:
            raise UserError(_('You cannot adjust inventory for this company.'))
        self.env.cr.execute('SELECT id FROM product_product WHERE id = %s FOR UPDATE', (self.id,))
        self.env.cr.execute(
            'SELECT id FROM stock_quant WHERE product_id = %s AND company_id = %s ORDER BY id FOR UPDATE',
            (self.id, company.id),
        )
        self.env.invalidate_all()

    def _check_inventory_valuation_integrity(self, company, cost_only_lot=None):
        """Check every valued serial; product totals alone can hide opposing gaps."""
        self.ensure_one()
        product = self.sudo().with_company(company).with_context(to_date=False)
        rounding = product.uom_id.rounding
        money = company.currency_id.rounding
        quants = self.env['stock.quant'].sudo().search([
            ('product_id', '=', self.id), ('company_id', '=', company.id),
            ('location_id.usage', 'in', ['internal', 'transit']), ('owner_id', '=', False),
        ])
        physical = sum(quants.mapped('quantity'))
        if any(float_compare(q.quantity, 0, precision_rounding=rounding) < 0 for q in quants):
            raise UserError(_('Negative physical stock must be reconciled before adjusting this product.'))
        if float_compare(physical, product.quantity_svl, precision_rounding=rounding):
            raise UserError(_('Physical stock %(physical)s differs from valuation quantity %(valued)s. Review Inventory Issues first.',
                              physical=physical, valued=product.quantity_svl))
        if product.cost_method == 'average':
            if product.value_svl < -money / 2 or product.standard_price < 0:
                raise UserError(_('Negative inventory value or Product Cost requires valuation review.'))
            if not float_compare(product.quantity_svl, 0, precision_rounding=rounding) and abs(product.value_svl) >= money / 2:
                raise UserError(_('Value remains on a product with no valued stock. Review Inventory Issues.'))
        if product.cost_method == 'average' and product.quantity_svl > 0 and not cost_only_lot and float_compare(
                product.standard_price, product.value_svl / product.quantity_svl, precision_rounding=money):
            raise UserError(_('Product Cost differs from the valuation average. Review Inventory Issues before changing quantity.'))
        if not product.lot_valuated:
            return
        layers = self.env['stock.valuation.layer'].sudo().search([
            ('product_id', '=', self.id), ('company_id', '=', company.id),
        ])
        lots = (layers.lot_id | quants.lot_id).with_company(company).with_context(to_date=False)
        for lot in lots:
            stock = sum(quants.filtered(lambda q: q.lot_id == lot).mapped('quantity'))
            qty, value = lot.quantity_svl, lot.value_svl
            if float_compare(stock, qty, precision_rounding=rounding):
                raise UserError(_('Serial/lot %(lot)s has physical quantity %(stock)s but valuation quantity %(qty)s.',
                                  lot=lot.display_name, stock=stock, qty=qty))
            if product.tracking == 'serial' and stock not in (0, 1):
                raise UserError(_('Serial %(lot)s is duplicated in physical stock.', lot=lot.display_name))
            if not float_compare(qty, 0, precision_rounding=rounding) and abs(value) >= money / 2:
                raise UserError(_('Serial/lot %(lot)s has value without stock. Review Inventory Issues.', lot=lot.display_name))
            if qty > 0 and not cost_only_lot:
                if value <= 0 or lot.standard_price <= 0 or float_compare(
                        lot.standard_price, lot.avg_cost, precision_rounding=money):
                    raise UserError(_('Serial/lot %(lot)s has an invalid or inconsistent cost. Correct its cost before changing quantity.',
                                      lot=lot.display_name))

    def _apply_counted_inventory(self, company, location, counted_quantity, lot=None, reason=None):
        """Private common posting path used by legacy count and delta tools.

        Existing approved positive costs are used. Unknown costs require the
        Count & Cost wizard. No direct physical quantity writes are permitted.
        """
        self.ensure_one()
        product = self.with_company(company).with_context(to_date=False, disable_auto_svl=False)
        lot = lot or self.env['stock.lot']
        if not product.is_storable or (product.company_id and product.company_id != company):
            raise UserError(_('Select a storable product for this company.'))
        if location.usage != 'internal' or (location.company_id and location.company_id != company):
            raise UserError(_('Counts require an internal location in the selected company. Use a transfer for transit stock.'))
        product.check_access('read')
        if lot:
            lot.check_access('read')
        location.check_access('read')
        if product.tracking != 'none' and (not lot or lot.product_id != product):
            raise UserError(_('Select the exact lot/serial for the product.'))
        if lot and (lot.product_id != product or (lot.company_id and lot.company_id != company)):
            raise UserError(_('The lot/serial belongs to another product or company.'))
        if product.tracking == 'none' and lot:
            raise UserError(_('An untracked product cannot have a lot/serial.'))
        if not math.isfinite(counted_quantity):
            raise UserError(_('Counted quantity must be a finite number.'))
        if counted_quantity < 0 or (product.tracking == 'serial' and counted_quantity not in (0, 1)):
            raise UserError(_('Counts cannot be negative; a serial must have a final count of 0 or 1.'))
        product._lock_inventory_adjustment(company)
        product._check_inventory_valuation_integrity(company)
        Quant = self.env['stock.quant'].sudo().with_company(company).with_context(to_date=False, disable_auto_svl=False)
        domain = [('product_id', '=', self.id), ('company_id', '=', company.id),
                  ('location_id', '=', location.id), ('lot_id', '=', lot.id or False)]
        quants = Quant.search(domain)
        if len(quants) > 1 or any(q.owner_id or q.package_id for q in quants):
            raise UserError(_('Owned, packaged, or duplicate stock rows require individual inventory review.'))
        previous = quants.quantity if quants else 0
        difference = counted_quantity - previous
        if not float_compare(difference, 0, precision_rounding=product.uom_id.rounding):
            return previous, previous
        if quants and quants.reserved_quantity:
            raise UserError(_('Release reservations before changing the count.'))
        if product.tracking == 'serial' and counted_quantity == 1 and Quant.search_count([
                ('product_id', '=', self.id), ('lot_id', '=', lot.id), ('company_id', '=', company.id),
                ('location_id', '!=', location.id), ('location_id.usage', 'in', ['internal', 'transit']),
                ('quantity', '>', 0)]):
            raise UserError(_('This serial is already in stock elsewhere. Use an internal transfer.'))
        cost = lot.with_company(company).standard_price if lot and product.lot_valuated else product.standard_price
        if difference > 0 and cost <= 0:
            raise UserError(_('Adding stock requires a positive documented cost. Use Count & Cost Adjustment.'))
        before_layers = product.sudo().stock_valuation_layer_ids.filtered(lambda s: s.company_id == company)
        before_qty, before_value, before_cost = product.sudo().quantity_svl, product.sudo().value_svl, product.sudo().standard_price
        if quants:
            quants.inventory_quantity = counted_quantity
        else:
            quants = Quant.with_context(inventory_mode=True).create({
                # Quant company is derived from its validated location. Odoo
                # rejects company_id in an inventory-mode create.
                'product_id': self.id, 'location_id': location.id,
                'lot_id': lot.id or False, 'inventory_quantity': counted_quantity,
            })
        if quants.with_context(inventory_name=reason or _('Controlled legacy inventory adjustment')).action_apply_inventory():
            raise UserError(_('Inventory conflict requires review. The adjustment was not posted.'))
        product._check_inventory_valuation_integrity(company)
        if float_compare(quants.quantity, counted_quantity, precision_rounding=product.uom_id.rounding):
            raise UserError(_('The posted count differs from the requested count.'))
        layers = product.sudo().stock_valuation_layer_ids.filtered(lambda s: s.company_id == company) - before_layers
        if product.valuation == 'real_time' and any(s.value and (not s.account_move_id or s.account_move_id.state != 'posted') for s in layers):
            raise UserError(_('The adjustment did not create its required accounting entries.'))
        self.env['stock.count.cost.log'].sudo().create({
            'name': reason or _('Legacy count adjustment'), 'company_id': company.id, 'product_id': self.id,
            'location_id': location.id, 'lot_id': lot.id or False, 'user_id': self.env.user.id,
            'performed_at': fields.Datetime.now(), 'reason': reason or _('Count using the existing product/serial cost'),
            'previous_quantity': previous, 'counted_quantity': counted_quantity, 'quantity_difference': difference,
            'previous_product_quantity': before_qty, 'previous_product_value': before_value,
            'previous_unit_cost': cost, 'previous_stored_cost': cost, 'final_unit_cost': cost,
            'previous_product_cost': before_cost, 'final_product_quantity': product.sudo().quantity_svl,
            'final_product_value': product.sudo().value_svl, 'final_product_cost': product.sudo().standard_price,
            'valuation_layer_ids': [(6, 0, layers.ids)],
        })
        return previous, quants.quantity

    def _sync_standard_price_from_valuation(self, company):
        """Align AVCO Product Cost after a valuation-only quantity correction.

        Writing ``standard_price`` through the ORM creates a new revaluation layer.
        That is correct for a deliberate revaluation, but wrong after we have just
        repaired the quantities/values of existing valuation layers.  In that case
        the valuation already contains the authoritative total, so update only the
        company-dependent cost cache without creating another valuation movement.
        """
        self.ensure_one()
        product = self.with_company(company).sudo()
        if product.categ_id.property_cost_method != 'average':
            return None

        self.env.cr.execute(
            """
                SELECT
                    COALESCE(SUM(quantity), 0.0),
                    COALESCE(SUM(value), 0.0)
                FROM stock_valuation_layer
                WHERE product_id = %s
                  AND company_id = %s
            """,
            (self.id, company.id),
        )
        valuation_qty, valuation_value = self.env.cr.fetchone()
        # A positive value divided by a negative valuation quantity would
        # poison Product Cost and every subsequent AVCO transaction.
        if float_compare(valuation_qty, 0.0, precision_rounding=product.uom_id.rounding) <= 0:
            return None

        unit_cost = valuation_value / valuation_qty
        if float_compare(unit_cost, 0.0, precision_rounding=company.currency_id.rounding) <= 0:
            return None
        self.env.cr.execute(
            """
                UPDATE product_product
                   SET standard_price = jsonb_set(
                           COALESCE(standard_price, '{}'::jsonb),
                           ARRAY[%s]::text[],
                           to_jsonb(%s::double precision),
                           TRUE
                       ),
                       write_uid = %s,
                       write_date = NOW()
                 WHERE id = %s
            """,
            (str(company.id), unit_cost, self.env.user.id, self.id),
        )
        self.invalidate_recordset(['standard_price'])
        return unit_cost

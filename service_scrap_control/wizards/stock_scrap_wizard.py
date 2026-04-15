from odoo import Command, _, api, fields, models
from odoo.exceptions import UserError


class StockScrapWizard(models.TransientModel):
    _name = 'stock.scrap.wizard'
    _description = 'Service Scrap Wizard'

    picking_id = fields.Many2one('stock.picking', required=True)
    vendor = fields.Boolean()
    line_ids = fields.One2many('scrap.line', 'wizard_id', string='Scrap Lines')

    @api.model
    def _line_qty_from_move_line(self, move_line):
        qty = float(getattr(move_line, 'quantity', 0.0) or 0.0)
        if qty <= 0:
            qty = float(getattr(move_line, 'qty_done', 0.0) or 0.0)
        if qty <= 0:
            qty = float(getattr(move_line, 'reserved_uom_qty', 0.0) or 0.0)
        return qty

    @api.model
    def _normalize_scrap_lines(self, line_dicts):
        normalized = {}
        for line in line_dicts:
            product = line['product']
            lot = line.get('lot')
            uom = line.get('uom') or product.uom_id
            owner = line.get('owner')
            package = line.get('package')
            qty = float(line.get('qty') or 0.0)
            if qty <= 0:
                continue
            key = (
                product.id,
                lot.id if lot else False,
                uom.id if uom else False,
                owner.id if owner else False,
                package.id if package else False,
            )
            if key in normalized:
                normalized[key]['qty'] += qty
            else:
                normalized[key] = {
                    'product': product,
                    'qty': qty,
                    'uom': uom,
                    'lot': lot,
                    'owner': owner,
                    'package': package,
                }
        return list(normalized.values())

    @api.model
    def _prepare_scrap_lines_from_move_lines(self, picking):
        if not hasattr(picking, 'move_line_ids_without_package'):
            return []

        lines = []
        move_lines = picking.move_line_ids_without_package.filtered(
            lambda line: line.state != 'cancel' and line.product_id
        )
        for move_line in move_lines:
            qty = self._line_qty_from_move_line(move_line)
            if qty <= 0:
                continue
            product = move_line.product_id
            lines.append({
                'product': product,
                'qty': qty,
                'uom': move_line.product_uom_id or product.uom_id,
                'lot': move_line.lot_id,
                'owner': move_line.owner_id,
                'package': move_line.package_id,
            })
        return self._normalize_scrap_lines(lines)

    @api.model
    def _prepare_scrap_lines_from_moves(self, picking):
        lines = []
        for move in picking.move_ids.filtered(lambda m: m.state != 'cancel' and m.product_id):
            quantity = float(move.product_uom_qty or 0.0)
            if quantity <= 0:
                continue
            product = move.product_id
            first_line = move.move_line_ids[:1]
            lines.append({
                'product': product,
                'qty': quantity,
                'uom': move.product_uom or product.uom_id,
                'lot': first_line.lot_id if first_line else self.env['stock.lot'],
                'owner': first_line.owner_id if first_line else self.env['res.partner'],
                'package': first_line.package_id if first_line else self.env['stock.quant.package'],
            })
        return self._normalize_scrap_lines(lines)

    @api.model
    def _prepare_default_scrap_lines(self, picking):
        # Odoo12 parity: seed from executed move lines first (lot-aware), then fallback to moves.
        lines = self._prepare_scrap_lines_from_move_lines(picking)
        if lines:
            return lines
        return self._prepare_scrap_lines_from_moves(picking)

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        picking_id = res.get('picking_id') or self.env.context.get('default_picking_id')
        if not picking_id:
            return res

        picking = self.env['stock.picking'].browse(picking_id)
        line_commands = []
        for line in self._prepare_default_scrap_lines(picking):
            line_commands.append(Command.create({
                'product_id': line['product'].id,
                'qty': line['qty'],
                'product_uom_id': line['uom'].id if line['uom'] else False,
                'lot_id': line['lot'].id if line['lot'] else False,
                'owner_id': line['owner'].id if line['owner'] else False,
                'package_id': line['package'].id if line['package'] else False,
            }))

        if line_commands:
            res['line_ids'] = line_commands
        return res

    def _get_effective_scrap_lines(self):
        self.ensure_one()
        lines = self.line_ids.filtered(lambda line: line.product_id and (line.qty or 0) > 0)
        return self._normalize_scrap_lines([{
            'product': line.product_id,
            'qty': line.qty,
            'uom': line.product_uom_id or line.product_id.uom_id,
            'lot': line.lot_id,
            'owner': line.owner_id,
            'package': line.package_id,
        } for line in lines])

    def action_create_scrap(self):
        self.ensure_one()
        lines = self._get_effective_scrap_lines()
        if not lines:
            raise UserError(_('Please keep at least one scrap line with product and positive quantity.'))

        picking = self.picking_id
        scrap_location = picking._get_service_scrap_location(vendor=self.vendor)
        if not scrap_location:
            raise UserError(_('No scrap destination location was found for this warehouse.'))

        source_location = picking.location_dest_id if picking.state == 'done' else picking.location_id
        scrap_state = 'approve' if (self.vendor or picking.approve_scrap) else 'draft'

        scraps = self.env['stock.scrap']
        for line in lines:
            if line['product'].tracking != 'none' and not line['lot']:
                raise UserError(
                    _('Please set Lot/Serial Number for tracked product: %(product)s')
                    % {'product': line['product'].display_name}
                )
            vals = {
                'picking_id': picking.id,
                'product_id': line['product'].id,
                'product_uom_id': line['uom'].id,
                'scrap_qty': line['qty'],
                'lot_id': line['lot'].id if line['lot'] else False,
                'owner_id': line['owner'].id if line['owner'] else False,
                'package_id': line['package'].id if line['package'] else False,
                'company_id': picking.company_id.id,
                'location_id': source_location.id,
                'scrap_location_id': scrap_location.id,
                'origin': picking.name,
                'vendor_scrap': self.vendor,
                'state': scrap_state,
            }
            scraps |= self.env['stock.scrap'].create(vals)

        for scrap in scraps:
            result = scrap.action_validate()
            if isinstance(result, dict):
                return result

        return {'type': 'ir.actions.act_window_close'}


class ScrapLine(models.TransientModel):
    _name = 'scrap.line'
    _description = 'Service Scrap Wizard Line'

    wizard_id = fields.Many2one('stock.scrap.wizard', required=True, ondelete='cascade')
    product_id = fields.Many2one('product.product')
    qty = fields.Float(string='Quantity', required=True, digits='Product Unit of Measure')
    product_uom_id = fields.Many2one('uom.uom')
    lot_id = fields.Many2one(
        'stock.lot',
        string='Lot/Serial',
        domain="[('product_id', '=', product_id)]",
    )
    owner_id = fields.Many2one('res.partner', string='Owner')
    package_id = fields.Many2one('stock.quant.package', string='Package')

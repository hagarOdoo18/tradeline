from odoo import Command, _, api, fields, models
from odoo.exceptions import UserError


class StockScrapWizard(models.TransientModel):
    _name = 'stock.scrap.wizard'
    _description = 'Service Scrap Wizard'

    picking_id = fields.Many2one('stock.picking', required=True)
    vendor = fields.Boolean()
    line_ids = fields.One2many('scrap.line', 'wizard_id', string='Scrap Lines')

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        picking_id = res.get('picking_id') or self.env.context.get('default_picking_id')
        if not picking_id:
            return res

        picking = self.env['stock.picking'].browse(picking_id)
        line_commands = []
        for move in picking.move_ids.filtered(lambda m: m.state != 'cancel' and m.product_id):
            quantity = float(move.product_uom_qty or 0.0)
            if not quantity and move.move_line_ids:
                quantity = sum(move.move_line_ids.mapped('quantity'))
            if quantity <= 0:
                continue

            first_line = move.move_line_ids[:1]
            line_commands.append(
                Command.create(
                    {
                        'product_id': move.product_id.id,
                        'qty': quantity,
                        'product_uom_id': move.product_uom.id,
                        'lot_id': first_line.lot_id.id if first_line else False,
                        'owner_id': first_line.owner_id.id if first_line else False,
                        'package_id': first_line.package_id.id if first_line else False,
                    }
                )
            )

        if line_commands:
            res['line_ids'] = line_commands
        return res

    def action_create_scrap(self):
        self.ensure_one()
        lines = self.line_ids.filtered(lambda line: line.qty > 0)
        if not lines:
            raise UserError(_('Please add at least one scrap line with a positive quantity.'))

        picking = self.picking_id
        scrap_location = picking._get_service_scrap_location(vendor=self.vendor)
        if not scrap_location:
            raise UserError(_('No scrap destination location was found for this warehouse.'))

        source_location = picking.location_dest_id if picking.state == 'done' else picking.location_id
        scrap_state = 'approve' if (self.vendor or picking.approve_scrap) else 'draft'

        scraps = self.env['stock.scrap']
        for line in lines:
            vals = {
                'picking_id': picking.id,
                'product_id': line.product_id.id,
                'product_uom_id': line.product_uom_id.id,
                'scrap_qty': line.qty,
                'lot_id': line.lot_id.id,
                'owner_id': line.owner_id.id,
                'package_id': line.package_id.id,
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
    product_id = fields.Many2one('product.product', required=True)
    qty = fields.Float(string='Quantity', required=True, digits='Product Unit of Measure')
    product_uom_id = fields.Many2one('uom.uom', required=True)
    lot_id = fields.Many2one(
        'stock.lot',
        string='Lot/Serial',
        domain="[('product_id', '=', product_id)]",
    )
    owner_id = fields.Many2one('res.partner', string='Owner')
    package_id = fields.Many2one('stock.quant.package', string='Package')

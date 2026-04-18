from odoo import models, fields, api, _
from odoo.exceptions import UserError
from odoo.tools import float_compare


class StockScrapWizard(models.TransientModel):
    _name = 'stock.scrap.wizard'
    _description = 'Stock Scrap Wizard'

    vendor = fields.Boolean(string='Vendor')
    scrap_lines = fields.One2many(
        comodel_name='scrap.line',
        inverse_name='scrap_id',
        string='Scrap Lines',
    )
    picking_id = fields.Many2one('stock.picking', string='Picking', readonly=True)
    location_id = fields.Many2one(
        'stock.location', string='Location',
        domain="[('usage', '=', 'internal')]",
        required=True, readonly=True)
    scrap_location_id = fields.Many2one(
        'stock.location', string='Scrap Location',
        domain="[('scrap_location', '=', True)]",
        required=True, readonly=True)

    def prepare_scrap_line(self, picking):
        product_scrap_moves = {}
        for picking_line in picking.move_line_ids_without_package:
            if picking_line.product_id.tracking == 'none':
                # Odoo 17+: qty_done was renamed to quantity on stock.move.line
                product_scrap_moves.setdefault(str(picking_line.product_id.id), picking_line.quantity)
            else:
                product_scrap_moves.setdefault(str(picking_line.lot_id.id), picking_line.product_id.id)
        return product_scrap_moves

    def set_scrap_lines(self, scrap_lines):
        lines = []
        for key in dict(scrap_lines).keys():
            product = self.env['product.product'].search([('id', '=', key)])
            if product:
                line_dec = (0, 0, {
                    'product_id': int(key),
                    'quantity': scrap_lines[key],
                    'product_uom_id': product.uom_id.id,
                })
            else:
                product = self.env['product.product'].search([('id', '=', scrap_lines[key])])
                line_dec = (0, 0, {
                    'lot_id': int(key),
                    'product_id': scrap_lines[key],
                    'quantity': 1,
                    'product_uom_id': product.uom_id.id,
                })
            lines.append(line_dec)
        return lines

    @api.model
    def default_get(self, fields_list):
        if len(self.env.context.get('active_ids', list())) > 1:
            raise UserError(_("You may only Scrap one picking at a time."))

        res = super().default_get(fields_list)
        picking = self.env['stock.picking'].browse(self.env.context.get('active_id'))

        scrap_lines = self.prepare_scrap_line(picking)
        res['scrap_lines'] = self.set_scrap_lines(scrap_lines)
        res['location_id'] = picking.location_dest_id.id

        company_id = picking.company_id .id

        if self.env.context.get('default_vendor'):
            vendor_loc = self.env['stock.location'].search(
                [('scrap_vendor_location', '=', True), ('company_id', 'in', [company_id, False])],
                limit=1)
            if vendor_loc:
                res['scrap_location_id'] = vendor_loc.id
        else:
            # if picking.picking_type_id.warehouse_id.id == 48:
            #     scrap_loc = self.env['stock.location'].search(
            #         [('scrap_location', '=', True), ('company_id', '=', company_id)],
            #         limit=1)
            #     if scrap_loc:
            #         res['scrap_location_id'] = scrap_loc.id
            # else:
            scraps = self.env['stock.location'].search(
                [('scrap_location', '=', True),  ('company_id', '=', company_id)])
            if len(scraps) > 1:
                res['scrap_location_id'] = scraps[1].id
            elif scraps:
                res['scrap_location_id'] = scraps[0].id
        return res

    def create_scrap(self):
        for line in self.scrap_lines:
            scrap = self.env['stock.scrap'].create({
                'product_id': line.product_id.id,
                'product_uom_id': line.product_id.uom_id.id,
                'lot_id': line.lot_id.id,
                'scrap_qty': line.quantity,
                'location_id': self.location_id.id,
                'scrap_location_id': self.scrap_location_id.id,
                'origin': self.picking_id.name,
            })
            # Odoo 18: product type 'product' was removed;
            # storable products are now type='consu' with is_storable=True.
            if not scrap.product_id.is_storable:
                scrap.action_validate()
                continue

            precision = self.env['decimal.precision'].precision_get('Product Unit of Measure')
            available_qty = sum(self.env['stock.quant']._gather(
                scrap.product_id,
                scrap.location_id,
                lot_id=scrap.lot_id,
                package_id=scrap.package_id,
                owner_id=scrap.owner_id,
                strict=True,
            ).mapped('quantity'))
            scrap_qty = scrap.product_uom_id._compute_quantity(scrap.scrap_qty, scrap.product_id.uom_id)

            if float_compare(available_qty, scrap_qty, precision_digits=precision) >= 0:
                scrap.action_validate()
                continue
            else:
                if scrap.product_id.tracking != 'none':
                    raise UserError(_(
                        'This product [%(prod)s] does not have QTY with this Serial [%(lot)s]',
                        prod=scrap.product_id.name, lot=scrap.lot_id.name))
                else:
                    raise UserError(_(
                        'This product %(prod)s does not have QTY', prod=scrap.product_id.name))

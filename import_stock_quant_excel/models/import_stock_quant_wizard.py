from odoo import models, fields, api, _
from odoo.exceptions import UserError
from odoo.tools import float_compare
import base64
from io import BytesIO
import openpyxl

class ImportStockQuantWizard(models.TransientModel):
    _name = 'import.stock.quant.wizard'
    _description = 'Import Stock Quant Wizard'

    file = fields.Binary(required=True)
    filename = fields.Char()
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company, required=True)
    state = fields.Selection([('draft','Draft'),('preview','Preview'),('done','Done')], default='draft')
    line_ids = fields.One2many('import.stock.quant.line','wizard_id')

    def write(self, vals):
        if {'file', 'company_id'}.intersection(vals):
            if any(w.state == 'done' for w in self):
                raise UserError(_('Applied imports cannot be reused.'))
            vals = dict(vals, state='draft')
        return super().write(vals)

    def action_preview(self):
        self.ensure_one()
        if self.state == 'done':
            raise UserError(_('An applied import cannot be reused.'))
        self.line_ids.unlink()
        data = base64.b64decode(self.file)
        wb = openpyxl.load_workbook(BytesIO(data), data_only=True)
        sheet = wb.active

        Product = self.env['product.product'].with_company(self.company_id)
        Location = self.env['stock.location']
        Lot = self.env['stock.lot']

        for row in range(2, sheet.max_row + 1):
            qty = float(sheet.cell(row,3).value or 0)
            vals = {
                'wizard_id': self.id,
                'row_no': row,
                'location_name': sheet.cell(row,1).value,
                'item_code': sheet.cell(row,2).value,
                'quantity': qty,
                'serial': sheet.cell(row,4).value,
                'is_valid': True,
            }

            if not vals['location_name'] or not vals['item_code'] or qty == 0:
                vals.update({'is_valid':False,'error_msg':'Missing data or zero qty'})
                self.env['import.stock.quant.line'].create(vals)
                continue

            location = Location.search([
                ('complete_name','=',vals['location_name']),
                ('usage','=','internal'),
                ('company_id','in',[self.company_id.id, False])
            ], limit=1)

            product = Product.search([('barcode','=',vals['item_code'])], limit=1)

            if not location or not product:
                vals.update({'is_valid':False,'error_msg':'Product or location not found'})
                self.env['import.stock.quant.line'].create(vals)
                continue

            vals.update({'location_id':location.id,'product_id':product.id})

            if vals['serial']:
                if product.tracking == 'serial' and abs(qty) != 1:
                    vals.update({'is_valid':False,'error_msg':'Serial qty must be 1 or -1'})
                lot = Lot.search([
                    ('name','=',vals['serial']),
                    ('product_id','=',product.id),
                    ('company_id','in',[self.company_id.id, False])
                ], limit=1)
                if lot:
                    vals['lot_id'] = lot.id
                elif vals['is_valid']:
                    vals.update({'is_valid': False, 'error_msg': 'Serial Not Found'})
            elif product.tracking != 'none':
                vals.update({'is_valid': False, 'error_msg': 'Lot/serial number required'})

            quants = self.env['stock.quant'].search([('product_id', '=', product.id),
                ('location_id', '=', location.id), ('company_id', '=', self.company_id.id),
                ('lot_id', '=', vals.get('lot_id', False))])
            vals['previous_quantity'] = sum(quants.mapped('quantity'))
            vals['projected_quantity'] = vals['previous_quantity'] + qty
            self.env['import.stock.quant.line'].create(vals)

        self.state = 'preview'

        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }
    def create_lots(self):
        self.ensure_one()
        if self.state != 'preview':
            raise UserError(_('Preview the import first.'))
        for rec in self.line_ids:
            if rec.serial and not rec.lot_id and rec.product_id and rec.location_id and rec.error_msg == 'Serial Not Found':
                lot = self.env['stock.lot'].create({
                    'name': rec.serial,
                    'product_id': rec.product_id.id,
                    'company_id': self.company_id.id,
                })
                rec.lot_id = lot.id
                rec.is_valid = True
                rec.error_msg = 'Serial Created'
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    def action_remove_invalid_lines(self):
        self.line_ids.filtered(lambda l: not l.is_valid).unlink()

    def action_apply(self):
        self.ensure_one()
        if self.line_ids.filtered(lambda l: not l.is_valid):
            raise UserError(_('Fix errors before applying'))

        if self.state != 'preview' or not self.line_ids:
            raise UserError(_('Preview a non-empty import before applying it.'))
        self.env.cr.execute('SELECT id FROM import_stock_quant_wizard WHERE id = %s FOR UPDATE', (self.id,))
        self.invalidate_recordset()
        if self.state != 'preview':
            raise UserError(_('This import has already been applied.'))
        products = self.line_ids.product_id.sorted('id')
        for product in products:
            product._lock_inventory_adjustment(self.company_id)
        seen = set()
        for line in self.line_ids:
            key = (line.product_id.id, line.location_id.id, line.lot_id.id)
            if key in seen:
                raise UserError(_('Duplicate product/location/serial rows must be combined before importing.'))
            seen.add(key)
            if not line.product_id or not line.location_id:
                raise UserError(_('Every row requires a product and location.'))
            quant = self.env['stock.quant'].search([
                ('product_id', '=', line.product_id.id), ('location_id', '=', line.location_id.id),
                ('company_id', '=', self.company_id.id), ('lot_id', '=', line.lot_id.id or False)])
            if len(quant) > 1:
                raise UserError(_('Duplicate stock rows require review.'))
            current = quant.quantity if quant else 0
            if float_compare(current, line.previous_quantity, precision_rounding=line.product_id.uom_id.rounding):
                raise UserError(_('Stock changed since Preview at row %(row)s. Preview the file again.', row=line.row_no))
            line.product_id._apply_counted_inventory(
                self.company_id, line.location_id, current + line.quantity, lot=line.lot_id,
                reason=_('Excel stock adjustment: %(file)s, row %(row)s', file=self.filename or '', row=line.row_no))

        self.state = 'done'


class ImportStockQuantLine(models.TransientModel):
    _name = 'import.stock.quant.line'
    _description = 'Import Stock Quant Line'

    wizard_id = fields.Many2one('import.stock.quant.wizard', ondelete='cascade')
    row_no = fields.Integer()
    location_name = fields.Char()
    item_code = fields.Char()
    quantity = fields.Float(string='Quantity to Add / Subtract')
    previous_quantity = fields.Float(string='Quantity Before', readonly=True)
    projected_quantity = fields.Float(string='Expected Quantity', readonly=True)
    serial = fields.Char()
    product_id = fields.Many2one('product.product')
    location_id = fields.Many2one('stock.location')
    lot_id = fields.Many2one('stock.lot')
    is_valid = fields.Boolean(default=True)
    error_msg = fields.Text()

    def create_lot(self):
        self.ensure_one()
        if self.wizard_id.state != 'preview':
            raise UserError(_('Preview the import first.'))
        if self.serial and not self.lot_id and self.product_id and self.location_id and self.error_msg == 'Serial Not Found':
            lot = self.env['stock.lot'].create({
                'name': self.serial,
                'product_id': self.product_id.id,
                'company_id': self.wizard_id.company_id.id,
            })
            self.lot_id = lot.id
            self.is_valid = True
            self.error_msg = 'Serial Creadted'
        return {
            "type": "ir.actions.act_window",
            "res_model": self.wizard_id._name,
            "res_id": self.wizard_id.id,
            "view_mode": "form",
            "target": "new",
        }



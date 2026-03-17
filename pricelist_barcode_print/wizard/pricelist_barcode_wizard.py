from odoo import _, api, fields, models
from odoo.exceptions import UserError


class PricelistBarcodePrintWizard(models.TransientModel):
    _name = 'pricelist.barcode.print.wizard'
    _description = 'Print Barcode Labels from Pricelist'

    pricelist_id = fields.Many2one('product.pricelist', string='Pricelist', required=True, readonly=True)
    currency_id  = fields.Many2one(related='pricelist_id.currency_id', readonly=True)
    select_all   = fields.Boolean(string='Select All', default=True)
    line_ids     = fields.One2many('pricelist.barcode.print.wizard.line', 'wizard_id', string='Products')

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        active_id = self.env.context.get('active_id')
        if not active_id:
            return res

        pricelist = self.env['product.pricelist'].browse(active_id)
        res['pricelist_id'] = pricelist.id

        lines = []
        seen = set()

        for item in pricelist.item_ids.filtered(lambda i: i.applied_on == '1_product' and i.product_tmpl_id):
            product = item.product_tmpl_id.product_variant_ids.filtered('barcode')[:1] \
                      or item.product_tmpl_id.product_variant_ids[:1]
            if not product or product.id in seen:
                continue
            seen.add(product.id)
            lines.append({'product_id': product.id, 'price': item.fixed_price,
                          'label_qty': 1, 'selected': True, 'has_barcode': bool(product.barcode)})

        for item in pricelist.item_ids.filtered(lambda i: i.applied_on == '0_product_variant' and i.product_id):
            product = item.product_id
            if product.id in seen:
                continue
            seen.add(product.id)
            lines.append({'product_id': product.id, 'price': item.fixed_price,
                          'label_qty': 1, 'selected': True, 'has_barcode': bool(product.barcode)})

        res['line_ids'] = [(0, 0, v) for v in lines]
        return res

    @api.onchange('select_all')
    def _onchange_select_all(self):
        for line in self.line_ids:
            line.selected = self.select_all

    def action_set_qty_all(self):
        qty = self.env.context.get('qty', 1)
        for line in self.line_ids.filtered('selected'):
            line.label_qty = qty
        return {'type': 'ir.actions.act_window', 'res_model': self._name,
                'res_id': self.id, 'view_mode': 'form', 'target': 'new'}

    def action_print(self):
        selected = self.line_ids.filtered(lambda l: l.selected and l.label_qty > 0)
        if not selected:
            raise UserError(_('No lines selected. Please select at least one product and set a label quantity.'))

        # Serialize all data NOW while the transient records are still alive.
        # The report renderer runs in a separate request/transaction where
        # TransientModel rows may already have been garbage-collected.
        lines_data = [
            {
                'product_id':     line.product_id.id,
                'price':          line.price,
                'label_qty':      int(line.label_qty),
                'currency_symbol': self.currency_id.symbol or '',
            }
            for line in selected
        ]

        return self.env.ref(
            'pricelist_barcode_print.action_report_pricelist_barcode_label'
        ).report_action(self, data={'lines': lines_data})

    def action_close(self):
        return {'type': 'ir.actions.act_window_close'}


class PricelistBarcodePrintWizardLine(models.TransientModel):
    _name = 'pricelist.barcode.print.wizard.line'
    _description = 'Pricelist Barcode Print Wizard Line'
    _order = 'product_id'

    wizard_id       = fields.Many2one('pricelist.barcode.print.wizard', ondelete='cascade')
    selected        = fields.Boolean(string='Print', default=True)
    product_id      = fields.Many2one('product.product', string='Product', required=True, readonly=True)
    product_barcode = fields.Char(related='product_id.barcode', string='Barcode', readonly=True)
    has_barcode     = fields.Boolean(readonly=True)
    price           = fields.Float(string='Price', digits='Product Price')
    label_qty       = fields.Integer(string='# Labels', default=1)


class ReportPricelistBarcodeLabel(models.AbstractModel):
    """
    Abstract report model – Odoo calls _get_report_values() when rendering.
    We receive the serialized `data['lines']` that was packed in action_print()
    and re-browse the permanent product.product records so the template can
    use t-field / barcode widget safely.
    """
    _name = 'report.pricelist_barcode_print.pricelist_barcode_label_template'
    _description = 'Pricelist Barcode Label Report'

    def _get_report_values(self, docids, data=None):
        data = data or {}
        lines_raw = data.get('lines', [])

        print_lines = []
        for item in lines_raw:
            product = self.env['product.product'].browse(item['product_id'])
            print_lines.append({
                'product':         product,
                'price':           item['price'],
                'label_qty':       item['label_qty'],
                'currency_symbol': item.get('currency_symbol', ''),
            })

        return {
            'doc_ids':    docids,
            'doc_model':  'pricelist.barcode.print.wizard',
            'print_lines': print_lines,
        }

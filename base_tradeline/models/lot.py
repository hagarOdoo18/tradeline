from odoo import fields, models, api,_
from odoo.exceptions import ValidationError

class StockLot(models.Model):
    _inherit = 'stock.lot'

    item_code = fields.Char(
        string='Item code',related="product_id.barcode",
        required=False)

    @api.constrains('name', 'product_id', 'company_id')
    def _check_unique_lot(self):
        domain = [('product_id', 'in', self.product_id.ids),
                  ('name', 'in', self.mapped('name'))]
        groupby = ['company_id', 'name']
        if any(not lot.company_id for lot in self):
            # We need to check across other companies to not have duplicates between 'no-company' and a company.
            self = self.sudo()
        records = self._read_group(domain, groupby, ['__count'], order='company_id DESC')
        error_message_lines = set()
        cross_lots = {}
        for company, product, name, count in records:
            if not company:
                cross_lots[(product, name)] = count
            # For company-specific lots, we check that there is no duplicate with 'no-company' lots, but NOT between specific-company ones.
            if (company and (cross_lots.get((product, name), 0) + count) > 1) or count > 1:
                error_message_lines.add(
                    _(" - Product: %(product)s, Lot/Serial Number: %(lot)s", product=product.display_name, lot=name))
        if error_message_lines:
            raise ValidationError(
                _(
                    "The combination of lot/serial number and product must be unique within a company including when no company is defined.\nThe following combinations contain duplicates:\n%(error_lines)s",
                    error_lines="\n".join(error_message_lines),
                ),
            )
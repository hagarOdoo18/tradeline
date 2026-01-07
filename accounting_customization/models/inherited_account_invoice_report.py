# Part of BrowseInfo. See LICENSE file for full copyright and licensing details.

from odoo import fields, models,api
from odoo.tools.sql import SQL
class AccountInvoiceReport(models.Model):
    _inherit = "account.invoice.report"

    branch_id = fields.Many2one('res.branch')

    reference_number = fields.Char(
        string='Reference Number',
        required=False)

    invoice_number = fields.Char(
        string='Invoice Number',
        required=False)
    preferred_payment_method_line_id = fields.Many2one(
        string="Payment Method",
        comodel_name='account.payment.method.line',
        store=True,
        readonly=False,
    )

    pricelist_id = fields.Many2one('product.pricelist', string='Pricelist')

    discount_id = fields.Many2one(
        comodel_name='discount.reason',
        string='Discount Reason',
        required=False)

    family_id = fields.Many2one(
        comodel_name='product.family',
        string='Family',
        required=False)


    sales_rep_id = fields.Many2one(
        comodel_name='sales.rep',
        string='Sales Rep',
        required=False)

    price_subtotal_currency = fields.Float(string='Untaxed Amount in Currency', groups='accounting_customization.group_accounting_reporting_price_subtotal_currency',readonly=True)
    price_subtotal = fields.Float(string='Untaxed Amount',groups='accounting_customization.group_accounting_reporting_price_subtotal' , readonly=True)
    price_total = fields.Float(string='Total in Currency',groups='accounting_customization.group_accounting_reporting_Total_Currency', readonly=True)
    price_average = fields.Float(string='Average Price', groups='accounting_customization.group_accounting_reporting_avarage',readonly=True, aggregator="avg")
    price_margin = fields.Float(string='Margin', groups='accounting_customization.group_accounting_reporting_Margin' ,readonly=True)
    inventory_value = fields.Float(string='Inventory Value', groups='accounting_customization.group_accounting_reporting_valuation', readonly=True)


    def _select(self) -> SQL:
            return SQL("%s, move.branch_id AS branch_id, move.reference_number AS reference_number,move.name AS invoice_number "
                       ",move.preferred_payment_method_line_id as preferred_payment_method_line_id,move.pricelist_id as pricelist_id,"
                       "move.discount_id as discount_id,line.family_id,move.sales_rep_id as sales_rep_id",
                       super()._select())


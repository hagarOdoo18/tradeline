# Part of BrowseInfo. See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models
from odoo.tools.sql import SQL


UNTAX_COST_DIVISOR = 1.14


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
    sub_categ_id = fields.Many2one(
        comodel_name='sub.category',
        string='Sub Category',
        required=False)
    sales_rep_id = fields.Many2one(
        comodel_name='sales.rep',
        string='Sales Rep',
        required=False)
    vendor_id = fields.Many2one(
        comodel_name='res.partner',
        string='Vendor',
        required=False)
    inventory_cost_method = fields.Selection(
        selection=[
            ('standard', 'Standard'),
            ('average', 'Average (AVCO)'),
            ('fifo', 'FIFO'),
        ],
        string='Cost Method',
        readonly=True,
    )
    inventory_unit_cost_used = fields.Float(
        string='Unit Cost Used',
        readonly=True,
        aggregator="avg",
    )
    inventory_unit_cost_untaxed_used = fields.Float(
        string='Unit Cost Untaxed Used',
        readonly=True,
        aggregator="avg",
    )
    point = fields.Integer(
        string='Point',
        required=False)


    price_subtotal_currency = fields.Float(string='Untaxed Amount in Currency', groups='accounting_customization.group_accounting_reporting_price_subtotal_currency',readonly=True)
    price_subtotal = fields.Float(string='Untaxed Amount',groups='accounting_customization.group_accounting_reporting_price_subtotal' , readonly=True)
    price_total = fields.Float(string='Total in Currency',groups='accounting_customization.group_accounting_reporting_Total_Currency', readonly=True)
    price_average = fields.Float(string='Average Price', groups='accounting_customization.group_accounting_reporting_avarage',readonly=True, aggregator="avg")
    price_margin = fields.Float(string='Margin', groups="accounting_customization.group_accounting_reporting_old_Margin",readonly=True)
    inventory_value = fields.Float(string='Inventory Value', groups='accounting_customization.group_accounting_reporting_valuation', readonly=True)
    inventory_value_untaxed = fields.Float(
        string="Inventory Value (Untaxed)",
        groups="accounting_customization.group_accounting_reporting_valuation",
        readonly=True,
    )
    price_margin_taxed = fields.Float(
        string="Net Margin (UNTaxed)",groups='accounting_customization.group_accounting_reporting_Margin' ,
        readonly=True
    )
    sales_margin_untaxed = fields.Float(
        string="Sales Margin",
        groups='accounting_customization.group_accounting_reporting_Margin',
        readonly=True
    )
    credit_note_impact_untaxed = fields.Float(
        string="Credit Note Impact",
        groups='accounting_customization.group_accounting_reporting_Margin',
        readonly=True
    )
    price_total_converted = fields.Float(
        string='Total Invoice (Company Currency)',
        groups='accounting_customization.group_accounting_reporting_Total_Currency',
        readonly=True,
    )



    def _select(self) -> SQL:
            cost_qty_expr = "(line.quantity / NULLIF(COALESCE(uom_line.factor, 1) / COALESCE(uom_template.factor, 1), 0.0))"
            std_price_expr = "COALESCE(product.standard_price -> line.company_id::text, to_jsonb(0.0))::float"
            untaxed_cost_expr = f"({cost_qty_expr} * ({std_price_expr} / {UNTAX_COST_DIVISOR}))"

            return SQL(
                "%s, "
                "move.branch_id AS branch_id, "
                "move.reference_number AS reference_number, "
                "move.name AS invoice_number, "
                "move.preferred_payment_method_line_id as preferred_payment_method_line_id, "
                "move.pricelist_id as pricelist_id, "
                "move.discount_id as discount_id, "
                "line.family_id, "
                "line.sub_categ_id as sub_categ_id, "
                "move.sales_rep_id as sales_rep_id, "
                "line.product_point as point, "
                "product.vendor_id as vendor_id, "
                "COALESCE(( "
                "  SELECT pc.property_cost_method ->> line.company_id::text "
                "  FROM product_product pp2 "
                "  JOIN product_template pt2 ON pt2.id = pp2.product_tmpl_id "
                "  JOIN product_category pc ON pc.id = pt2.categ_id "
                "  WHERE pp2.id = line.product_id "
                "), 'standard') AS inventory_cost_method, "
                "%s AS inventory_unit_cost_used, "
                "(%s / %s) AS inventory_unit_cost_untaxed_used, "
                "CASE "
                "  WHEN move.move_type NOT IN ('out_invoice', 'out_receipt', 'out_refund') THEN 0.0 "
                "  WHEN move.move_type = 'out_refund' THEN account_currency_table.rate * (-1 * %s) "
                "  ELSE account_currency_table.rate * (%s) "
                "END AS inventory_value_untaxed, "
                "CASE "
                "  WHEN move.move_type NOT IN ('out_invoice', 'out_receipt', 'out_refund') THEN 0.0 "
                "  WHEN move.move_type = 'out_refund' THEN account_currency_table.rate * (-line.balance + %s) "
                "  ELSE account_currency_table.rate * (-line.balance - %s) "
                "END AS price_margin_taxed, "
                "CASE "
                "  WHEN move.move_type IN ('out_invoice', 'out_receipt') "
                "  THEN account_currency_table.rate * (-line.balance - %s) "
                "  ELSE 0.0 "
                "END AS sales_margin_untaxed, "
                "CASE "
                "  WHEN move.move_type = 'out_refund' "
                "  THEN account_currency_table.rate * (-line.balance + %s) "
                "  ELSE 0.0 "
                "END AS credit_note_impact_untaxed, "
                "CASE "
                "  WHEN move.move_type IN ('in_invoice', 'out_refund', 'in_receipt') "
                "  THEN ((line.price_total / NULLIF(COALESCE(move.invoice_currency_rate, 1), 0)) "
                "        * account_currency_table.rate) * -1 "
                "  ELSE (line.price_total / NULLIF(COALESCE(move.invoice_currency_rate, 1), 0)) "
                "       * account_currency_table.rate "
                "END AS price_total_converted ",
                super()._select(),
                SQL(std_price_expr),
                SQL(std_price_expr),
                SQL(str(UNTAX_COST_DIVISOR)),
                SQL(untaxed_cost_expr),
                SQL(untaxed_cost_expr),
                SQL(untaxed_cost_expr),
                SQL(untaxed_cost_expr),
                SQL(untaxed_cost_expr),
                SQL(untaxed_cost_expr),
            )

from odoo import fields, models


class LegacyInvoiceLine(models.Model):
    _inherit = "legacy.invoice.line"

    source_customer_id = fields.Integer(index=True)
    source_customer_name = fields.Char(index=True)
    customer_mobile = fields.Char(index=True)
    customer_phone = fields.Char(index=True)
    source_upc = fields.Char(index=True)

    source_salesperson_id = fields.Integer(index=True)
    source_salesperson_name = fields.Char(index=True)

    source_line_team_id = fields.Integer(index=True)
    source_line_team_name = fields.Char(index=True)
    source_branch_name = fields.Char(
        related="source_line_team_name",
        string="Branch",
        store=True,
        index=True,
    )
    source_store_name = fields.Char(
        related="source_line_team_name",
        string="Store",
        store=True,
        index=True,
    )

    source_opportunity_id = fields.Integer(index=True)
    source_opportunity_name = fields.Char(index=True)
    source_cc_rep_id = fields.Integer(index=True)
    source_cc_rep_name = fields.Char(index=True)

    source_po_invoice = fields.Char(index=True)

    source_channel = fields.Char(index=True)
    source_channel_id = fields.Integer(index=True)
    source_courier = fields.Char(index=True)
    source_warranty = fields.Char(index=True)
    source_additional_code = fields.Char(index=True)

    source_payment_journal_line = fields.Char(index=True)
    journal_summary_line = fields.Char(index=True)
    payment_amount_line = fields.Float()
    payment_type = fields.Char(index=True)

    serial_items = fields.Text()

    quantity_signed = fields.Float()
    price_unit_signed = fields.Monetary(currency_field="currency_id")
    price_subtotal_signed = fields.Monetary(currency_field="currency_id")
    price_total_signed = fields.Monetary(currency_field="currency_id")


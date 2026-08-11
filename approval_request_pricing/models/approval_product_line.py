from odoo import fields, models


class ApprovalProductLine(models.Model):
    _inherit = "approval.product.line"

    currency_id = fields.Many2one(
        comodel_name="res.currency",
        related="company_id.currency_id",
        store=True,
        readonly=True,
    )
    unit_cost = fields.Monetary(
        string="Unit Cost",
        currency_field="currency_id",
    )
    selling_price = fields.Monetary(
        string="Selling Price",
        currency_field="currency_id",
    )
    margin = fields.Monetary(
        string="Margin",
        currency_field="currency_id",
    )

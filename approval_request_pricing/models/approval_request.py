from odoo import api, fields, models


class ApprovalRequest(models.Model):
    _inherit = "approval.request"

    payment_term_option_id = fields.Many2one(
        comodel_name="approval.payment.term.option",
        string="Payment Terms",
        ondelete="restrict",
    )
    method_type_option_id = fields.Many2one(
        comodel_name="approval.method.type.option",
        string="Method Type",
        ondelete="restrict",
    )
    pricing_currency_id = fields.Many2one(
        comodel_name="res.currency",
        related="company_id.currency_id",
        readonly=True,
    )
    total_cost = fields.Monetary(
        string="Total Cost",
        currency_field="pricing_currency_id",
        compute="_compute_pricing_totals",
        store=True,
    )
    total_selling = fields.Monetary(
        string="Total Selling",
        currency_field="pricing_currency_id",
        compute="_compute_pricing_totals",
        store=True,
    )
    total_margin = fields.Monetary(
        string="Total Margin",
        currency_field="pricing_currency_id",
        compute="_compute_pricing_totals",
        store=True,
    )

    @api.depends(
        "product_line_ids.quantity",
        "product_line_ids.unit_cost",
        "product_line_ids.selling_price",
    )
    def _compute_pricing_totals(self):
        for request in self:
            request.total_cost = sum(
                line.quantity * line.unit_cost for line in request.product_line_ids
            )
            request.total_selling = sum(
                line.quantity * line.selling_price
                for line in request.product_line_ids
            )
            request.total_margin = request.total_selling - request.total_cost

from odoo import api, fields, models


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
        compute="_compute_unit_cost",
        store=True,
        readonly=False,
    )
    selling_price = fields.Monetary(
        string="Selling Price",
        currency_field="currency_id",
        compute="_compute_selling_price",
        store=True,
        readonly=False,
    )
    margin = fields.Monetary(
        string="Margin",
        currency_field="currency_id",
        compute="_compute_margin",
        store=True,
    )

    @api.depends("product_id", "company_id")
    def _compute_unit_cost(self):
        for line in self:
            product = line.product_id
            if not product:
                line.unit_cost = 0.0
                continue

            if line.company_id:
                product = product.with_company(line.company_id)
            line.unit_cost = product.standard_price

    @api.depends("product_id", "company_id")
    def _compute_selling_price(self):
        for line in self:
            product = line.product_id
            if not product:
                line.selling_price = 0.0
                continue

            if line.company_id:
                product = product.with_company(line.company_id)
            line.selling_price = product.lst_price

    @api.depends("unit_cost", "selling_price")
    def _compute_margin(self):
        for line in self:
            line.margin = line.selling_price - line.unit_cost

    def _apply_approved_unit_cost_to_purchase_order_line(self):
        """Use the approved cost as the price of the generated RFQ line."""
        for line in self.filtered("purchase_order_line_id"):
            purchase_line = line.purchase_order_line_id
            source_currency = line.currency_id
            target_currency = purchase_line.currency_id
            unit_cost = line.unit_cost

            if source_currency and target_currency and source_currency != target_currency:
                unit_cost = source_currency._convert(
                    unit_cost,
                    target_currency,
                    line.company_id,
                    purchase_line.order_id.date_order or fields.Date.context_today(line),
                )

            purchase_line.price_unit = unit_cost

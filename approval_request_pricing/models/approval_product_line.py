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
        precompute=True,
    )
    selling_price = fields.Monetary(
        string="Selling Price",
        currency_field="currency_id",
        compute="_compute_selling_price",
        store=True,
        readonly=False,
        precompute=True,
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

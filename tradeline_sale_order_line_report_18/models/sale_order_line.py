from odoo import api, fields, models


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    number_order = fields.Char(
        string="Ref",
        related="order_id.name",
        store=True,
        readonly=True,
    )
    order_date = fields.Datetime(
        string="Order Date",
        related="order_id.date_order",
        store=True,
        readonly=True,
    )
    confirmation_date = fields.Datetime(
        string="Confirmation Date",
        compute="_compute_tradeline_confirmation_date",
        store=True,
        readonly=True,
    )
    mobile_customer = fields.Char(
        string="Mobile Number",
        related="order_partner_id.mobile",
        store=True,
        readonly=True,
    )
    phone_customer = fields.Char(
        string="Phone Customer",
        related="order_partner_id.phone",
        store=True,
        readonly=True,
    )
    user_id = fields.Many2one(
        string="Branch",
        related="order_id.user_id",
        store=True,
        readonly=True,
    )
    team_id = fields.Many2one(
        comodel_name="crm.team",
        string="Sales Team",
        related="order_id.team_id",
        store=True,
        readonly=True,
    )
    categ_product = fields.Char(
        string="Product Category",
        related="product_id.categ_id.complete_name",
        store=True,
        readonly=True,
    )
    product_categ_id = fields.Many2one(
        comodel_name="product.category",
        string="Product Category",
        related="product_id.categ_id",
        store=True,
        readonly=True,
    )
    description_product = fields.Html(
        string="Description",
        related="product_id.description",
        store=True,
        readonly=True,
    )
    item_code_product = fields.Char(
        string="Item Code",
        related="product_id.barcode",
        store=True,
        readonly=True,
    )
    upc = fields.Char(
        string="UPC",
        related="product_id.default_code",
        store=True,
        readonly=True,
    )
    quantity_product = fields.Float(
        string="Quantity",
        related="product_uom_qty",
        store=True,
        readonly=True,
    )
    amount_total_line = fields.Monetary(
        string="Total",
        related="price_total",
        currency_field="currency_id",
        store=True,
        readonly=True,
    )
    status_quotation = fields.Selection(
        string="State Quotation",
        related="state",
        store=True,
        readonly=True,
    )
    status_sale = fields.Selection(
        string="Invoice Status",
        related="invoice_status",
        store=True,
        readonly=True,
    )
    vendor_name_product = fields.Char(
        string="Vendor",
        compute="_compute_tradeline_product_vendor",
        store=True,
        readonly=True,
    )

    @api.depends("order_id.date_order", "state")
    def _compute_tradeline_confirmation_date(self):
        for line in self:
            line.confirmation_date = line.order_id.date_order if line.state in ("sale", "done") else False

    @api.depends("product_id.product_tmpl_id.seller_ids.partner_id.name")
    def _compute_tradeline_product_vendor(self):
        for line in self:
            seller = line.product_id.product_tmpl_id.seller_ids[:1]
            line.vendor_name_product = seller.partner_id.display_name if seller else False

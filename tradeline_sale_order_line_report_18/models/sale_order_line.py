import re

from odoo import api, fields, models


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    seq = fields.Char(
        string="Seq",
        compute="_compute_seq",
        store=True,
        readonly=True,
    )

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
    store_name = fields.Char(
        string="Store",
        compute="_compute_store_name",
        readonly=True,
    )
    sales_rep_name = fields.Char(
        string="Sales Rep",
        compute="_compute_sales_rep_name",
        readonly=True,
    )
    family_name = fields.Char(
        string="Family",
        compute="_compute_family_name",
        readonly=True,
    )
    warranty_name = fields.Char(
        string="Warranty",
        compute="_compute_warranty_name",
        readonly=True,
    )
    payment_amount = fields.Monetary(
        string="Payment Amount",
        currency_field="currency_id",
        compute="_compute_payment_financials",
        readonly=True,
    )
    amount_due = fields.Monetary(
        string="Amount Due",
        currency_field="currency_id",
        compute="_compute_payment_financials",
        readonly=True,
    )
    payment_journal = fields.Char(
        string="Payment Journal",
        compute="_compute_payment_journal",
        readonly=True,
    )
    note = fields.Html(
        string="Note",
        related="order_id.note",
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

    @api.depends("state", "invoice_status", "order_id.name")
    def _compute_seq(self):
        for line in self:
            workflow_prefix = "I" if (line.state in ("sale", "done") or line.invoice_status in ("to invoice", "invoiced")) else "Q"
            order_ref = line.order_id.name or line.number_order or ""
            number = re.sub(r"\D", "", order_ref)
            line.seq = "%s-%s" % (workflow_prefix, number or str(line.order_id.id or line.id or "0"))

    def _compute_store_name(self):
        for line in self:
            line.store_name = line.order_id.team_id.display_name or False

    def _compute_sales_rep_name(self):
        for line in self:
            if "sales_rep_id" in line.order_id._fields and line.order_id.sales_rep_id:
                line.sales_rep_name = line.order_id.sales_rep_id.display_name
            else:
                line.sales_rep_name = False

    def _compute_family_name(self):
        for line in self:
            value = False
            if "family_id" in line._fields and line.family_id:
                value = line.family_id.display_name
            elif "family_id" in line.product_id._fields and line.product_id.family_id:
                value = line.product_id.family_id.display_name
            line.family_name = value

    def _compute_warranty_name(self):
        for line in self:
            value = False
            if "warranty_id" in line._fields and line.warranty_id:
                value = line.warranty_id.display_name
            elif "warranty_id" in line.product_id._fields and line.product_id.warranty_id:
                value = line.product_id.warranty_id.display_name
            line.warranty_name = value

    def _compute_payment_financials(self):
        for line in self:
            order = line.order_id
            payment_amount = 0.0
            amount_due = 0.0
            if order:
                if "amount_paid" in order._fields:
                    payment_amount = order.amount_paid or 0.0
                elif "amount_invoiced" in order._fields:
                    payment_amount = order.amount_invoiced or 0.0

                if "amount_due" in order._fields:
                    amount_due = order.amount_due or 0.0
                elif "amount_total" in order._fields and "amount_paid" in order._fields:
                    amount_due = (order.amount_total or 0.0) - (order.amount_paid or 0.0)
                elif "amount_to_invoice" in order._fields:
                    amount_due = order.amount_to_invoice or 0.0
            line.payment_amount = payment_amount
            line.amount_due = amount_due

    def _compute_payment_journal(self):
        for line in self:
            names = []
            order = line.order_id
            if order:
                if "payment_ids" in order._fields:
                    names = [n for n in order.payment_ids.mapped("journal_id.display_name") if n]
                elif "journal_id" in order._fields and order.journal_id:
                    names = [order.journal_id.display_name]
            seen = set()
            ordered_unique = []
            for name in names:
                if name not in seen:
                    seen.add(name)
                    ordered_unique.append(name)
            line.payment_journal = ", ".join(ordered_unique)

    @api.depends("order_id.date_order", "state")
    def _compute_tradeline_confirmation_date(self):
        for line in self:
            line.confirmation_date = line.order_id.date_order if line.state in ("sale", "done") else False

    @api.depends("product_id.product_tmpl_id.seller_ids.partner_id.name")
    def _compute_tradeline_product_vendor(self):
        for line in self:
            seller = line.product_id.product_tmpl_id.seller_ids[:1]
            line.vendor_name_product = seller.partner_id.display_name if seller else False

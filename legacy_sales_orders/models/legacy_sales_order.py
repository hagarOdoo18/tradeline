from odoo import api, fields, models


class LegacySaleOrder(models.Model):
    _name = "legacy.sale.order"
    _description = "Legacy Sale Order"
    _order = "date_order desc, id desc"
    _rec_name = "name"

    active = fields.Boolean(default=True)

    source_db = fields.Char(required=True, index=True)
    source_order_id = fields.Integer(required=True, index=True)
    source_model = fields.Char(default="sale.order", required=True, index=True)

    name = fields.Char(required=True, index=True)
    state = fields.Char(index=True)
    date_order = fields.Datetime(index=True)
    confirmation_date = fields.Datetime(index=True)
    validity_date = fields.Date(index=True)

    source_partner_id = fields.Integer(index=True)
    source_partner_name = fields.Char(index=True)
    source_partner_code = fields.Char(index=True)
    source_partner_type = fields.Char(index=True)
    source_partner_mobile = fields.Char(index=True)
    source_partner_phone = fields.Char(index=True)
    source_partner_tax_id = fields.Char(index=True)
    source_partner_national_id = fields.Char(index=True)

    source_user_id = fields.Integer(index=True)
    source_user_name = fields.Char(index=True)
    source_team_id = fields.Integer(index=True)
    source_team_name = fields.Char(index=True)

    source_sales_rep_id = fields.Integer(index=True)
    source_sales_rep_name = fields.Char(index=True)
    source_sales_rep_code = fields.Char(index=True)

    source_opportunity_id = fields.Integer(index=True)
    source_opportunity_name = fields.Char(index=True)
    source_cc_rep_id = fields.Integer(index=True)
    source_cc_rep_name = fields.Char(index=True)

    source_origin = fields.Char(index=True)
    source_client_ref = fields.Char(index=True)
    source_po_ref = fields.Char(index=True)
    legacy_po_id = fields.Many2one("legacy.purchase.order", ondelete="set null", index=True)

    source_channel = fields.Char(index=True)
    source_courier = fields.Char(index=True)
    source_quotation_type = fields.Char(index=True)
    source_payment_journal_summary = fields.Char(index=True)
    source_tradeline_month = fields.Char(index=True)
    source_tradeline_year = fields.Char(index=True)

    company_id = fields.Many2one("res.company", ondelete="set null", index=True)
    currency_id = fields.Many2one("res.currency", ondelete="set null")
    source_currency_name = fields.Char(index=True)
    source_currency_rate = fields.Float()

    amount_untaxed = fields.Monetary(currency_field="currency_id")
    amount_tax = fields.Monetary(currency_field="currency_id")
    amount_total = fields.Monetary(currency_field="currency_id")

    note = fields.Text()
    import_batch_id = fields.Char(index=True)
    imported_at = fields.Datetime(default=fields.Datetime.now, index=True)
    legacy_payload = fields.Json()

    line_ids = fields.One2many("legacy.sale.order.line", "order_id", string="Lines")
    line_count = fields.Integer(compute="_compute_line_count", store=True)

    _sql_constraints = [
        (
            "legacy_sale_order_source_uniq",
            "unique(source_db, source_order_id)",
            "Legacy sale order source identity must be unique.",
        ),
    ]

    @api.depends("line_ids")
    def _compute_line_count(self):
        for record in self:
            record.line_count = len(record.line_ids)

    def action_open_legacy_po(self):
        self.ensure_one()
        if not self.legacy_po_id:
            return False
        action = self.env["ir.actions.actions"]._for_xml_id("legacy_invoice_archive.action_legacy_purchase_order")
        form_view = self.env.ref("legacy_invoice_archive.view_legacy_purchase_order_form", raise_if_not_found=False)
        if form_view:
            action["views"] = [(form_view.id, "form")]
        action.update(
            {
                "res_id": self.legacy_po_id.id,
                "view_mode": "form",
                "target": "current",
            }
        )
        return action


class LegacySaleOrderLine(models.Model):
    _name = "legacy.sale.order.line"
    _description = "Legacy Sale Order Line"
    _order = "order_date desc, id desc"

    order_id = fields.Many2one("legacy.sale.order", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="order_id.company_id", store=True, index=True)
    currency_id = fields.Many2one(related="order_id.currency_id", store=True)

    source_db = fields.Char(related="order_id.source_db", store=True, index=True)
    source_order_id = fields.Integer(related="order_id.source_order_id", store=True, index=True)
    source_line_id = fields.Integer(required=True, index=True)
    sequence = fields.Integer(default=10)

    order_name = fields.Char(related="order_id.name", store=True, index=True)
    state = fields.Char(related="order_id.state", store=True, index=True)
    order_date = fields.Datetime(index=True)
    confirmation_date = fields.Datetime(index=True)

    source_customer_id = fields.Integer(index=True)
    source_customer_name = fields.Char(index=True)
    customer_mobile = fields.Char(index=True)
    customer_phone = fields.Char(index=True)

    source_salesperson_id = fields.Integer(index=True)
    source_salesperson_name = fields.Char(index=True)
    source_line_team_id = fields.Integer(index=True)
    source_line_team_name = fields.Char(index=True)
    source_branch_name = fields.Char(index=True)
    source_store_name = fields.Char(index=True)

    source_sales_rep_id = fields.Integer(index=True)
    source_sales_rep_name = fields.Char(index=True)
    source_sales_rep_code = fields.Char(index=True)
    source_opportunity_id = fields.Integer(index=True)
    source_opportunity_name = fields.Char(index=True)
    source_cc_rep_id = fields.Integer(index=True)
    source_cc_rep_name = fields.Char(index=True)

    source_po_ref = fields.Char(related="order_id.source_po_ref", store=True, index=True)
    legacy_po_id = fields.Many2one(related="order_id.legacy_po_id", store=True, index=True)

    source_channel = fields.Char(index=True)
    source_courier = fields.Char(index=True)
    source_warranty = fields.Char(index=True)
    source_additional_code = fields.Char(index=True)
    source_payment_journal_line = fields.Char(index=True)
    payment_amount_line = fields.Monetary(currency_field="currency_id")
    payment_type = fields.Char(index=True)

    product_source_id = fields.Integer(index=True)
    item_code = fields.Char(index=True)
    source_upc = fields.Char(index=True)
    product_name = fields.Char(index=True)
    description = fields.Text()
    source_product_category_id = fields.Integer(index=True)
    product_category_name = fields.Char(index=True)
    source_family_id = fields.Integer(index=True)
    source_family_name = fields.Char(index=True)
    source_vendor_name = fields.Char(index=True)

    uom_name = fields.Char(index=True)
    qty_ordered = fields.Float()
    qty_delivered = fields.Float()
    qty_invoiced = fields.Float()
    price_unit = fields.Monetary(currency_field="currency_id")
    discount = fields.Float()
    tax_text = fields.Text()
    price_subtotal = fields.Monetary(currency_field="currency_id")
    price_total = fields.Monetary(currency_field="currency_id")
    amount_total_line = fields.Monetary(currency_field="currency_id")

    status_quotation = fields.Char(index=True)
    status_sale = fields.Char(index=True)

    legacy_payload = fields.Json()

    _sql_constraints = [
        (
            "legacy_sale_order_line_source_uniq",
            "unique(order_id, source_line_id)",
            "Legacy sale order line source identity must be unique per sale order.",
        ),
    ]

    def get_formview_action(self, access_uid=None):
        self.ensure_one()
        if self.order_id:
            return self.order_id.get_formview_action(access_uid=access_uid)
        return super().get_formview_action(access_uid=access_uid)

    def action_open_order(self):
        self.ensure_one()
        if not self.order_id:
            return False
        action = self.env["ir.actions.actions"]._for_xml_id("legacy_sales_orders.action_legacy_sale_quotation")
        form_view = self.env.ref("legacy_sales_orders.view_legacy_sale_order_form", raise_if_not_found=False)
        if form_view:
            action["views"] = [(form_view.id, "form")]
        action.update(
            {
                "res_id": self.order_id.id,
                "view_mode": "form",
                "target": "current",
            }
        )
        return action

    def action_open_legacy_po(self):
        self.ensure_one()
        if not self.legacy_po_id:
            return False
        action = self.env["ir.actions.actions"]._for_xml_id("legacy_invoice_archive.action_legacy_purchase_order")
        form_view = self.env.ref("legacy_invoice_archive.view_legacy_purchase_order_form", raise_if_not_found=False)
        if form_view:
            action["views"] = [(form_view.id, "form")]
        action.update(
            {
                "res_id": self.legacy_po_id.id,
                "view_mode": "form",
                "target": "current",
            }
        )
        return action

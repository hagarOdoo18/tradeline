import base64
import csv
from datetime import datetime
import html
import io
from collections import OrderedDict

from odoo import api, fields, models, tools
from odoo.exceptions import UserError


SUPPORTED_TEXT_OPERATORS = ("=", "!=", "like", "ilike", "not like", "not ilike", "=like", "=ilike")


class LegacyInvoice(models.Model):
    _name = "legacy.invoice"
    _description = "Legacy Invoice Archive"
    _order = "invoice_date desc, id desc"
    _rec_name = "number"

    active = fields.Boolean(default=True)

    source_db = fields.Char(required=True, index=True)
    source_model = fields.Char(required=True, default="account.invoice", index=True)
    source_id = fields.Integer(required=True, index=True)
    source_partner_id = fields.Integer(index=True)
    source_partner_name = fields.Char()
    source_discount_reason_id = fields.Integer(index=True)
    discount_reason_raw = fields.Char()
    discount_reason_normalized = fields.Char(index=True)
    discount_reason_source = fields.Selection(
        selection=[
            ("raw", "Raw"),
            ("inferred", "Inferred"),
            ("none", "None"),
        ],
        default="none",
        index=True,
    )
    discount_reason_inferred = fields.Boolean(default=False, index=True)

    number = fields.Char(index=True)
    source_name = fields.Char(index=True)
    source_reference_number = fields.Char(index=True)
    source_po_ref = fields.Char(index=True)
    source_offer = fields.Boolean(default=False, index=True)
    source_sms_sent = fields.Boolean(default=False, index=True)
    source_tradeline_month = fields.Char(index=True)
    source_tradeline_year = fields.Char(index=True)
    source_currency_rate = fields.Float()
    source_journal_id = fields.Integer(index=True)
    source_journal_code = fields.Char(index=True)
    source_journal_name = fields.Char(index=True)
    source_payment_term_id = fields.Integer(index=True)
    source_payment_term_name = fields.Char(index=True)
    source_fiscal_position_id = fields.Integer(index=True)
    source_fiscal_position_name = fields.Char(index=True)
    source_partner_bank_id = fields.Integer(index=True)
    source_partner_bank_name = fields.Char(index=True)
    source_user_id = fields.Integer(index=True)
    source_user_name = fields.Char(index=True)
    source_team_id = fields.Integer(index=True)
    source_team_name = fields.Char(index=True)
    source_sales_rep_id = fields.Integer(index=True)
    source_sales_rep_name = fields.Char(index=True)
    invoice_type = fields.Selection(
        selection=[
            ("out_invoice", "Customer Invoice"),
            ("out_refund", "Credit Note"),
            ("in_invoice", "Vendor Bill"),
            ("in_refund", "Vendor Credit Note"),
            ("other", "Other"),
        ],
        default="out_invoice",
        index=True,
    )
    state = fields.Selection(
        selection=[
            ("draft", "Draft"),
            ("open", "Open"),
            ("paid", "Paid"),
            ("cancel", "Cancelled"),
            ("proforma", "Pro-Forma"),
            ("proforma2", "Pro-Forma 2"),
            ("other", "Other"),
        ],
        default="open",
        index=True,
    )

    partner_id = fields.Many2one("res.partner", ondelete="set null", index=True)
    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company.id, index=True)
    currency_id = fields.Many2one("res.currency", required=True, default=lambda self: self.env.company.currency_id.id)
    source_currency_name = fields.Char(string="Original Currency", readonly=True, help="The explicit string name of the currency this invoice was billed in Odoo 12.")

    invoice_date = fields.Date(index=True)
    due_date = fields.Date()

    amount_untaxed = fields.Monetary(currency_field="currency_id")
    amount_tax = fields.Monetary(currency_field="currency_id")
    amount_total = fields.Monetary(currency_field="currency_id", index=True)
    amount_residual = fields.Monetary(currency_field="currency_id")

    comment = fields.Text()
    print_notes = fields.Text()
    notes_pos = fields.Text()
    product_notes = fields.Text()

    import_batch_id = fields.Char(index=True)
    imported_at = fields.Datetime(default=fields.Datetime.now, index=True)

    legacy_payload = fields.Json()

    line_ids = fields.One2many("legacy.invoice.line", "invoice_id", string="Lines")
    tax_line_ids = fields.One2many("legacy.invoice.tax.line", "invoice_id", string="Tax Lines")
    payment_link_ids = fields.One2many("legacy.invoice.payment.link", "invoice_id", string="Payment Links")
    attachment_ids = fields.One2many("legacy.invoice.attachment", "invoice_id", string="Attachments")
    serial_ref_ids = fields.One2many("legacy.invoice.serial.ref", "invoice_id", string="Serial References")
    customer_bridge_ids = fields.One2many("legacy.invoice.customer.bridge", "invoice_id", string="Customer Links")

    line_count = fields.Integer(compute="_compute_counts", store=True)
    attachment_count = fields.Integer(compute="_compute_counts", store=True)
    product_summary = fields.Char(compute="_compute_summaries", store=True, index=True)
    serial_summary = fields.Char(compute="_compute_summaries", store=True, index=True)
    payment_method_summary = fields.Char(compute="_compute_summaries", store=True, index=True)
    payment_journal_summary = fields.Char(compute="_compute_summaries", store=True, index=True)
    smart_search = fields.Char(
        string="Smart Search",
        compute="_compute_smart_search",
        search="_search_smart_search",
        store=False,
    )

    _sql_constraints = [
        (
            "legacy_invoice_source_uniq",
            "unique(source_db, source_model, source_id)",
            "The source invoice identity must be unique.",
        ),
    ]

    @api.depends("line_ids", "attachment_ids")
    def _compute_counts(self):
        for record in self:
            record.line_count = len(record.line_ids)
            record.attachment_count = len(record.attachment_ids)

    @api.depends(
        "line_ids.name",
        "line_ids.item_code",
        "line_ids.discount_reason_normalized",
        "serial_ref_ids.lot_name",
        "serial_ref_ids.item_code",
        "payment_link_ids.payment_method_name",
        "payment_link_ids.payment_method_code",
        "payment_link_ids.journal_name",
        "payment_link_ids.journal_code",
    )
    def _compute_summaries(self):
        def _join_unique(values):
            seen = set()
            out = []
            for value in values:
                text = (value or "").strip()
                if not text or text in seen:
                    continue
                seen.add(text)
                out.append(text)
            return ", ".join(out[:20])

        for record in self:
            product_parts = list(record.line_ids.mapped("name")) + list(record.line_ids.mapped("item_code"))
            serial_parts = list(record.serial_ref_ids.mapped("lot_name")) + list(record.serial_ref_ids.mapped("item_code"))
            method_parts = list(record.payment_link_ids.mapped("payment_method_name")) + list(
                record.payment_link_ids.mapped("payment_method_code")
            )
            journal_parts = list(record.payment_link_ids.mapped("journal_name")) + list(record.payment_link_ids.mapped("journal_code"))

            record.product_summary = _join_unique(product_parts)
            record.serial_summary = _join_unique(serial_parts)
            record.payment_method_summary = _join_unique(method_parts)
            record.payment_journal_summary = _join_unique(journal_parts)

    def _compute_smart_search(self):
        for record in self:
            record.smart_search = ""

    def _search_smart_search(self, operator, value):
        value = (value or "").strip()
        if not value:
            return []
        return [
            "|",
            "|",
            "|",
            "|",
            "|",
            "|",
            "|",
            "|",
            "|",
            "|",
            ("number", operator, value),
            ("source_name", operator, value),
            ("source_partner_name", operator, value),
            ("partner_id.name", operator, value),
            ("line_ids.name", operator, value),
            ("line_ids.item_code", operator, value),
            ("line_ids.discount_reason_normalized", operator, value),
            ("serial_ref_ids.lot_name", operator, value),
            ("serial_ref_ids.item_code", operator, value),
            ("payment_link_ids.reference", operator, value),
            ("payment_link_ids.payment_method_name", operator, value),
        ]

    def get_gift_invoice(self):
        self.ensure_one()
        return False

    def get_print_lines_summary(self):
        self.ensure_one()
        lines = self.line_ids.sorted(lambda l: (l.sequence, l.id))
        amount_words = ""
        try:
            if self.currency_id:
                amount_words = self.currency_id.amount_to_text(self.amount_total or 0.0)
        except Exception:
            amount_words = ""
        return {
            "printed_lines": lines,
            "printed_untaxed": self.amount_untaxed or 0.0,
            "printed_tax": self.amount_tax or 0.0,
            "printed_total": self.amount_total or 0.0,
            "printed_amount_words_en": amount_words,
        }

    def get_grouped_lot_values(self):
        self.ensure_one()
        grouped: OrderedDict[str, dict] = OrderedDict()
        line_name_by_code = {}
        for line in self.line_ids:
            if line.item_code and line.item_code not in line_name_by_code:
                line_name_by_code[line.item_code] = line.name or line.item_code

        for serial in self.serial_ref_ids:
            key = serial.item_code or serial.lot_name or "Unknown"
            if key not in grouped:
                grouped[key] = {
                    "product_name": line_name_by_code.get(key) or key,
                    "quantity": 0.0,
                    "uom_name": "",
                    "serials": [],
                    "serial_set": set(),
                }
            grouped[key]["quantity"] += serial.qty_done or 0.0
            if serial.lot_name and serial.lot_name not in grouped[key]["serial_set"]:
                grouped[key]["serials"].append(serial.lot_name)
                grouped[key]["serial_set"].add(serial.lot_name)

        output = []
        for item in grouped.values():
            output.append(
                {
                    "product_name": item["product_name"],
                    "quantity": item["quantity"],
                    "uom_name": item["uom_name"],
                    "serials": ", ".join(item["serials"]),
                }
            )
        return output


class LegacyInvoiceLine(models.Model):
    _name = "legacy.invoice.line"
    _description = "Legacy Invoice Line"
    _order = "invoice_id, sequence, id"

    invoice_id = fields.Many2one("legacy.invoice", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="invoice_id.company_id", store=True, index=True)
    currency_id = fields.Many2one(related="invoice_id.currency_id", store=True)
    company_currency_id = fields.Many2one(related="invoice_id.company_id.currency_id", store=True, index=True)

    source_id = fields.Integer(required=True, index=True)
    source_move_line_id = fields.Integer(index=True)
    product_source_id = fields.Integer(index=True)
    source_discount_reason_id = fields.Integer(index=True)

    invoice_date = fields.Date(related="invoice_id.invoice_date", store=True, index=True)
    due_date = fields.Date(related="invoice_id.due_date", store=True, index=True)
    partner_id = fields.Many2one(related="invoice_id.partner_id", store=True, index=True)
    source_partner_name = fields.Char(related="invoice_id.source_partner_name", store=True, index=True)
    invoice_number = fields.Char(related="invoice_id.number", store=True, index=True)
    source_name = fields.Char(related="invoice_id.source_name", store=True, index=True)
    source_reference_number = fields.Char(related="invoice_id.source_reference_number", store=True, index=True)
    source_po_ref = fields.Char(related="invoice_id.source_po_ref", store=True, index=True)
    source_offer = fields.Boolean(related="invoice_id.source_offer", store=True, index=True)
    source_sms_sent = fields.Boolean(related="invoice_id.source_sms_sent", store=True, index=True)
    source_tradeline_month = fields.Char(related="invoice_id.source_tradeline_month", store=True, index=True)
    source_tradeline_year = fields.Char(related="invoice_id.source_tradeline_year", store=True, index=True)
    source_currency_rate = fields.Float(related="invoice_id.source_currency_rate", store=True)
    source_journal_id = fields.Integer(related="invoice_id.source_journal_id", store=True, index=True)
    source_journal_code = fields.Char(related="invoice_id.source_journal_code", store=True, index=True)
    source_journal_name = fields.Char(related="invoice_id.source_journal_name", store=True, index=True)
    source_payment_term_id = fields.Integer(related="invoice_id.source_payment_term_id", store=True, index=True)
    source_payment_term_name = fields.Char(related="invoice_id.source_payment_term_name", store=True, index=True)
    source_fiscal_position_id = fields.Integer(related="invoice_id.source_fiscal_position_id", store=True, index=True)
    source_fiscal_position_name = fields.Char(related="invoice_id.source_fiscal_position_name", store=True, index=True)
    source_partner_bank_id = fields.Integer(related="invoice_id.source_partner_bank_id", store=True, index=True)
    source_partner_bank_name = fields.Char(related="invoice_id.source_partner_bank_name", store=True, index=True)
    source_user_id = fields.Integer(related="invoice_id.source_user_id", store=True, index=True)
    source_user_name = fields.Char(related="invoice_id.source_user_name", store=True, index=True)
    source_team_id = fields.Integer(related="invoice_id.source_team_id", store=True, index=True)
    source_team_name = fields.Char(related="invoice_id.source_team_name", store=True, index=True)
    source_sales_rep_id = fields.Integer(related="invoice_id.source_sales_rep_id", store=True, index=True)
    source_sales_rep_name = fields.Char(related="invoice_id.source_sales_rep_name", store=True, index=True)
    source_db = fields.Char(related="invoice_id.source_db", store=True, index=True)
    invoice_type = fields.Selection(related="invoice_id.invoice_type", store=True, index=True)
    state = fields.Selection(related="invoice_id.state", store=True, index=True)
    payment_method_summary = fields.Char(related="invoice_id.payment_method_summary", store=True, index=True)
    payment_journal_summary = fields.Char(related="invoice_id.payment_journal_summary", store=True, index=True)

    sequence = fields.Integer(default=10)
    name = fields.Text()

    product_id = fields.Many2one("product.product", ondelete="set null")
    product_tmpl_id = fields.Many2one("product.template", ondelete="set null")
    product_search_text = fields.Char(
        string="Product",
        compute="_compute_product_search_text",
        search="_search_product_search_text",
        store=False,
    )
    product_category_id = fields.Many2one(related="product_tmpl_id.categ_id", store=True, index=True)
    cost_method = fields.Selection(
        selection=[
            ("standard", "Standard Price"),
            ("fifo", "First In First Out (FIFO)"),
            ("average", "Average Cost (AVCO)"),
        ],
        store=True,
        index=True,
    )
    item_code = fields.Char(index=True)

    quantity = fields.Float()
    uom_id = fields.Many2one("uom.uom", ondelete="set null")
    price_unit = fields.Monetary(currency_field="currency_id")
    discount = fields.Float()
    discount_reason_raw = fields.Char()
    discount_reason_normalized = fields.Char(index=True)
    discount_reason_source = fields.Selection(
        selection=[
            ("raw", "Raw"),
            ("inferred", "Inferred"),
            ("none", "None"),
        ],
        default="none",
        index=True,
    )
    discount_reason_inferred = fields.Boolean(default=False, index=True)
    price_subtotal = fields.Monetary(currency_field="currency_id")
    price_total = fields.Monetary(currency_field="currency_id")
    amount_untaxed = fields.Monetary(
        string="Untaxed Amount",
        currency_field="currency_id",
        compute="_compute_analysis_amounts",
        store=True,
    )
    amount_tax = fields.Monetary(
        string="Tax Amount",
        currency_field="currency_id",
        compute="_compute_analysis_amounts",
        store=True,
    )
    amount_total = fields.Monetary(
        string="Total Amount",
        currency_field="currency_id",
        compute="_compute_analysis_amounts",
        store=True,
    )

    account_id = fields.Many2one("account.account", ondelete="set null")
    source_account_analytic_id = fields.Integer(index=True)
    source_account_analytic_name = fields.Char(index=True)
    source_family_id = fields.Integer(index=True)
    source_family_name = fields.Char(index=True)
    source_line_sales_rep_id = fields.Integer(index=True)
    source_line_sales_rep_name = fields.Char(index=True)
    source_pricelist_id = fields.Integer(index=True)
    source_pricelist_name = fields.Char(index=True)
    source_vendor_id = fields.Integer(index=True)
    source_vendor_name = fields.Char(index=True)
    source_sub_category_id = fields.Integer(index=True)
    source_sub_category_name = fields.Char(index=True)
    source_uom_name = fields.Char()
    source_tax_text = fields.Text()
    product_incentive = fields.Float()
    product_point = fields.Float()

    legacy_payload = fields.Json()

    _sql_constraints = [
        (
            "legacy_invoice_line_source_uniq",
            "unique(invoice_id, source_id)",
            "The source line identity must be unique per invoice.",
        ),
    ]

    @api.depends("product_id")
    def _compute_product_search_text(self):
        for line in self:
            line.product_search_text = line.product_id.display_name or ""

    @api.depends("price_subtotal", "price_total")
    def _compute_analysis_amounts(self):
        for line in self:
            untaxed = line.price_subtotal or 0.0
            total = line.price_total or 0.0
            line.amount_untaxed = untaxed
            line.amount_total = total
            line.amount_tax = total - untaxed

    def _search_product_search_text(self, operator, value):
        value = (value or "").strip()
        if not value:
            return []

        product_matches = self.env["product.product"].name_search(
            name=value,
            operator=operator,
            limit=5000,
        )
        product_ids = [product_id for product_id, _name in product_matches]
        if not product_ids:
            return [("id", "=", 0)]
        return [("product_id", "in", product_ids)]

    @api.model
    def _rewrite_product_id_text_domain(self, domain):
        if not domain:
            return domain

        def _rewrite_leaf(term):
            if not isinstance(term, (list, tuple)) or len(term) < 3:
                return term
            field_name, operator, value = term[0], term[1], term[2]
            if field_name != "product_id":
                return term
            if operator not in SUPPORTED_TEXT_OPERATORS:
                return term
            if not isinstance(value, str):
                return term
            value = value.strip()
            if not value:
                return term

            product_matches = self.env["product.product"].name_search(
                name=value,
                operator=operator,
                limit=5000,
            )
            product_ids = [product_id for product_id, _name in product_matches]
            if not product_ids:
                return ("id", "=", 0)
            return ("product_id", "in", product_ids)

        def _rewrite(node):
            if isinstance(node, tuple):
                return _rewrite_leaf(node)
            if isinstance(node, list):
                return [_rewrite(item) for item in node]
            return node

        return _rewrite(domain)

    @api.model
    def search(self, domain, offset=0, limit=None, order=None):
        domain = self._rewrite_product_id_text_domain(domain)
        return super().search(domain, offset=offset, limit=limit, order=order)

    @api.model
    def read_group(
        self,
        domain,
        fields,
        groupby,
        offset=0,
        limit=None,
        orderby=False,
        lazy=True,
    ):
        domain = self._rewrite_product_id_text_domain(domain)
        return super().read_group(
            domain,
            fields,
            groupby,
            offset=offset,
            limit=limit,
            orderby=orderby,
            lazy=lazy,
        )


class LegacyInvoiceTaxLine(models.Model):
    _name = "legacy.invoice.tax.line"
    _description = "Legacy Invoice Tax Line"
    _order = "invoice_id, id"

    invoice_id = fields.Many2one("legacy.invoice", required=True, ondelete="cascade", index=True)
    currency_id = fields.Many2one(related="invoice_id.currency_id", store=True)

    source_id = fields.Integer(required=True, index=True)

    name = fields.Char()
    tax_id = fields.Many2one("account.tax", ondelete="set null")
    tax_group_id = fields.Many2one("account.tax.group", ondelete="set null")

    amount = fields.Monetary(currency_field="currency_id")
    base_amount = fields.Monetary(currency_field="currency_id")

    legacy_payload = fields.Json()

    _sql_constraints = [
        (
            "legacy_invoice_tax_source_uniq",
            "unique(invoice_id, source_id)",
            "The source tax line identity must be unique per invoice.",
        ),
    ]


class LegacyInvoicePaymentLink(models.Model):
    _name = "legacy.invoice.payment.link"
    _description = "Legacy Invoice Payment Link"
    _order = "invoice_id, id"

    invoice_id = fields.Many2one("legacy.invoice", required=True, ondelete="cascade", index=True)
    currency_id = fields.Many2one(related="invoice_id.currency_id", store=True)

    source_invoice_id = fields.Integer(index=True)
    source_payment_id = fields.Integer(index=True)
    source_journal_id = fields.Integer(index=True)
    source_payment_method_id = fields.Integer(index=True)

    invoice_date = fields.Date(related="invoice_id.invoice_date", store=True, index=True)
    partner_id = fields.Many2one(related="invoice_id.partner_id", store=True, index=True)
    invoice_type = fields.Selection(related="invoice_id.invoice_type", store=True, index=True)
    state = fields.Selection(related="invoice_id.state", store=True, index=True)

    payment_id = fields.Many2one("account.payment", ondelete="set null")
    journal_id = fields.Many2one("account.journal", ondelete="set null")
    journal_code = fields.Char(index=True)
    journal_name = fields.Char(index=True)

    name = fields.Char()
    reference = fields.Char(index=True)
    payment_method_code = fields.Char(index=True)
    payment_method_name = fields.Char()
    payment_date = fields.Date(index=True)
    amount = fields.Monetary(currency_field="currency_id")

    legacy_payload = fields.Json()

    _sql_constraints = [
        (
            "legacy_invoice_payment_source_uniq",
            "unique(invoice_id, source_payment_id)",
            "The source payment link must be unique per invoice.",
        ),
    ]


class LegacyInvoiceAttachment(models.Model):
    _name = "legacy.invoice.attachment"
    _description = "Legacy Invoice Attachment"
    _order = "invoice_id, id"

    invoice_id = fields.Many2one("legacy.invoice", required=True, ondelete="cascade", index=True)

    source_attachment_id = fields.Integer(required=True, index=True)
    source_model = fields.Char(default="account.invoice")
    source_res_id = fields.Integer(index=True)

    name = fields.Char(required=True)
    datas_fname = fields.Char()
    mimetype = fields.Char()
    checksum = fields.Char(index=True)
    file_size = fields.Integer()
    store_fname = fields.Char()

    datas = fields.Binary(attachment=True)

    legacy_payload = fields.Json()

    _sql_constraints = [
        (
            "legacy_invoice_attachment_source_uniq",
            "unique(invoice_id, source_attachment_id)",
            "The source attachment identity must be unique per invoice.",
        ),
    ]


class LegacyInvoiceSerialRef(models.Model):
    _name = "legacy.invoice.serial.ref"
    _description = "Legacy Invoice Serial Reference"
    _order = "invoice_id, id"

    invoice_id = fields.Many2one("legacy.invoice", required=True, ondelete="cascade", index=True)
    invoice_line_id = fields.Many2one("legacy.invoice.line", ondelete="set null", index=True)

    source_move_line_id = fields.Integer(index=True)
    source_move_id = fields.Integer(index=True)
    source_picking_id = fields.Integer(index=True)
    source_lot_id = fields.Integer(index=True)

    invoice_date = fields.Date(related="invoice_id.invoice_date", store=True, index=True)
    partner_id = fields.Many2one(related="invoice_id.partner_id", store=True, index=True)
    invoice_type = fields.Selection(related="invoice_id.invoice_type", store=True, index=True)
    state = fields.Selection(related="invoice_id.state", store=True, index=True)

    lot_name = fields.Char(string="Lot Name / Serial Number", index=True)
    qty_done = fields.Float()
    item_code = fields.Char(index=True)
    product_category_id = fields.Many2one("product.category", ondelete="set null")

    picking_note = fields.Text()
    serial_excel_sheet = fields.Text()

    match_status = fields.Selection(
        selection=[
            ("legacy_only", "Legacy Only"),
            ("candidate", "Candidate"),
            ("ambiguous", "Ambiguous"),
            ("verified", "Verified"),
        ],
        default="legacy_only",
        index=True,
    )

    legacy_payload = fields.Json()

    _sql_constraints = [
        (
            "legacy_invoice_serial_ref_uniq",
            "unique(invoice_id, source_move_line_id, source_picking_id, source_lot_id, lot_name)",
            "Serial reference identity must be unique per invoice.",
        ),
    ]


class LegacyInvoiceCustomerBridge(models.Model):
    _name = "legacy.invoice.customer.bridge"
    _description = "Legacy Invoice Customer Bridge"
    _order = "id desc"

    invoice_id = fields.Many2one("legacy.invoice", ondelete="cascade", index=True)

    source_partner_id = fields.Integer(required=True, index=True)
    source_partner_name = fields.Char()
    source_vat = fields.Char(index=True)
    source_email = fields.Char(index=True)
    source_phone = fields.Char(index=True)

    target_partner_id = fields.Many2one("res.partner", ondelete="set null", index=True)

    match_method = fields.Selection(
        selection=[
            ("vat", "VAT"),
            ("email", "Email"),
            ("phone", "Phone"),
            ("ref", "Code/Ref"),
            ("manual", "Manual"),
        ],
        index=True,
    )
    match_status = fields.Selection(
        selection=[
            ("strict", "Strict"),
            ("ambiguous", "Ambiguous"),
            ("create_candidate", "Create Candidate"),
            ("manual_review", "Manual Review"),
            ("linked", "Linked"),
        ],
        required=True,
        default="manual_review",
        index=True,
    )

    confidence = fields.Float(digits=(16, 4))
    import_batch_id = fields.Char(index=True)
    note = fields.Text()

    _sql_constraints = [
        (
            "legacy_invoice_customer_bridge_uniq",
            "unique(invoice_id, source_partner_id, target_partner_id, match_method)",
            "Customer bridge must be unique for invoice and matched partner.",
        ),
    ]


class LegacyProduct(models.Model):
    _name = "legacy.product"
    _description = "Legacy Product"
    _order = "default_code, name, id"
    _rec_name = "display_name"

    source_db = fields.Char(required=True, index=True)
    source_product_id = fields.Integer(required=True, index=True)
    source_product_tmpl_id = fields.Integer(index=True)

    default_code = fields.Char(index=True)
    barcode = fields.Char(index=True)
    name = fields.Char(index=True)
    display_name = fields.Char(compute="_compute_display_name", store=True, index=True)

    product_category_id = fields.Many2one("product.category", ondelete="set null")
    product_category_name = fields.Char(index=True)
    source_brand_id = fields.Integer(index=True)
    source_brand_name = fields.Char(index=True)

    active = fields.Boolean(default=True, index=True)
    legacy_payload = fields.Json()

    _sql_constraints = [
        (
            "legacy_product_source_uniq",
            "unique(source_db, source_product_id)",
            "Legacy product source identity must be unique.",
        ),
    ]

    @api.depends("default_code", "name")
    def _compute_display_name(self):
        for record in self:
            if record.default_code:
                record.display_name = f"[{record.default_code}] {record.name or ''}".strip()
            else:
                record.display_name = record.name or ""


class LegacyProductMap(models.Model):
    _name = "legacy.product.map"
    _description = "Legacy Product Mapping"
    _order = "source_default_code, source_name, id"
    _rec_name = "source_name"

    source_db = fields.Char(required=True, index=True)
    source_product_id = fields.Integer(required=True, index=True)
    source_product_tmpl_id = fields.Integer(index=True)

    source_default_code = fields.Char(index=True)
    source_barcode = fields.Char(index=True)
    source_name = fields.Char(index=True)
    source_category_name = fields.Char(index=True)
    source_brand_name = fields.Char(index=True)

    target_product_id = fields.Many2one("product.product", ondelete="set null", index=True)
    target_product_tmpl_id = fields.Many2one(related="target_product_id.product_tmpl_id", store=True, index=True)

    match_status = fields.Selection(
        selection=[
            ("matched", "Matched"),
            ("review", "Needs Review"),
            ("unmatched", "Unmatched"),
        ],
        default="unmatched",
        required=True,
        index=True,
    )
    match_method = fields.Selection(
        selection=[
            ("auto_barcode", "Auto (Barcode)"),
            ("auto_code", "Auto (Item Code)"),
            ("manual", "Manual"),
            ("none", "None"),
        ],
        default="none",
        required=True,
        index=True,
    )
    confidence = fields.Float(digits=(16, 4))
    manual_override = fields.Boolean(default=False, index=True)
    note = fields.Text()

    _sql_constraints = [
        (
            "legacy_product_map_source_uniq",
            "unique(source_db, source_product_id)",
            "Legacy product mapping source identity must be unique.",
        ),
    ]

    def action_set_manual_match(self):
        for record in self:
            if not record.target_product_id:
                continue
            record.write(
                {
                    "match_status": "matched",
                    "match_method": "manual",
                    "manual_override": True,
                    "confidence": 1.0,
                }
            )


class LegacySerialLedger(models.Model):
    _name = "legacy.serial.ledger"
    _description = "Legacy Serial Ledger"
    _order = "movement_date desc, id desc"

    source_db = fields.Char(required=True, index=True)
    source_move_line_id = fields.Integer(required=True, index=True)
    source_move_id = fields.Integer(index=True)
    source_picking_id = fields.Integer(index=True)
    source_lot_id = fields.Integer(index=True)
    source_product_id = fields.Integer(index=True)
    invoice_source_id = fields.Integer(index=True)
    invoice_id = fields.Many2one("legacy.invoice", ondelete="set null", index=True)

    lot_name = fields.Char(string="Lot Name / Serial Number", index=True)
    item_code = fields.Char(index=True)
    product_name = fields.Char(index=True)
    product_category_id = fields.Many2one("product.category", ondelete="set null")
    product_category_name = fields.Char(index=True)

    qty_done = fields.Float()
    movement_date = fields.Datetime(index=True)
    movement_type = fields.Selection(
        selection=[
            ("in", "In"),
            ("out", "Out"),
            ("internal", "Internal"),
            ("adjustment", "Adjustment"),
            ("other", "Other"),
        ],
        default="other",
        index=True,
    )

    source_document = fields.Char(index=True)
    po_reference = fields.Char(index=True)
    origin = fields.Char(index=True)
    picking_name = fields.Char(index=True)
    warehouse_id = fields.Integer(index=True)
    warehouse_name = fields.Char(index=True)
    location_id = fields.Integer(index=True)
    location_dest_id = fields.Integer(index=True)
    location_usage = fields.Char(index=True)
    location_dest_usage = fields.Char(index=True)
    picking_note = fields.Text()

    legacy_payload = fields.Json()

    _sql_constraints = [
        (
            "legacy_serial_ledger_source_uniq",
            "unique(source_db, source_move_line_id)",
            "Serial ledger source move line must be unique.",
        ),
    ]


class LegacyProductCompare(models.Model):
    _name = "legacy.product.compare"
    _description = "Legacy vs Current Product Comparison"
    _auto = False
    _order = "legacy_sales_amount desc, id"

    source_db = fields.Char(readonly=True)
    source_product_id = fields.Integer(readonly=True)
    source_default_code = fields.Char(readonly=True)
    source_barcode = fields.Char(readonly=True)
    source_name = fields.Char(readonly=True)
    source_category_name = fields.Char(readonly=True)
    source_brand_name = fields.Char(readonly=True)

    target_product_id = fields.Many2one("product.product", readonly=True)
    target_product_tmpl_id = fields.Many2one("product.template", readonly=True)
    match_status = fields.Selection(
        selection=[
            ("matched", "Matched"),
            ("review", "Needs Review"),
            ("unmatched", "Unmatched"),
        ],
        readonly=True,
    )
    match_method = fields.Selection(
        selection=[
            ("auto_barcode", "Auto (Barcode)"),
            ("auto_code", "Auto (Item Code)"),
            ("manual", "Manual"),
            ("none", "None"),
        ],
        readonly=True,
    )
    confidence = fields.Float(readonly=True)

    legacy_sales_qty = fields.Float(readonly=True)
    legacy_sales_amount = fields.Float(readonly=True)
    current_sales_qty = fields.Float(readonly=True)
    current_sales_amount = fields.Float(readonly=True)
    current_stock_qty = fields.Float(compute="_compute_current_stock_qty", readonly=True)
    sales_amount_delta = fields.Float(compute="_compute_sales_delta", readonly=True)
    sales_qty_delta = fields.Float(compute="_compute_sales_delta", readonly=True)

    @api.depends("target_product_id")
    def _compute_current_stock_qty(self):
        for record in self:
            record.current_stock_qty = record.target_product_id.qty_available if record.target_product_id else 0.0

    @api.depends("legacy_sales_amount", "current_sales_amount", "legacy_sales_qty", "current_sales_qty")
    def _compute_sales_delta(self):
        for record in self:
            record.sales_amount_delta = (record.current_sales_amount or 0.0) - (record.legacy_sales_amount or 0.0)
            record.sales_qty_delta = (record.current_sales_qty or 0.0) - (record.legacy_sales_qty or 0.0)

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        
        self.env.cr.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'legacy_invoice_line' AND column_name = 'product_source_id'
        """)
        if not self.env.cr.fetchone():
            return

        self.env.cr.execute(
            """
            CREATE OR REPLACE VIEW legacy_product_compare AS
            WITH legacy_sales AS (
                SELECT
                    lil.product_source_id AS source_product_id,
                    SUM(COALESCE(lil.quantity, 0.0)) AS legacy_sales_qty,
                    SUM(COALESCE(lil.price_subtotal, 0.0)) AS legacy_sales_amount
                FROM legacy_invoice_line lil
                JOIN legacy_invoice li ON li.id = lil.invoice_id
                WHERE li.active = TRUE
                  AND li.state IN ('open', 'paid')
                  AND li.invoice_type IN ('out_invoice', 'out_refund')
                GROUP BY lil.product_source_id
            ),
            current_sales AS (
                SELECT
                    aml.product_id,
                    SUM(COALESCE(aml.quantity, 0.0)) AS current_sales_qty,
                    SUM(COALESCE(aml.price_subtotal, 0.0)) AS current_sales_amount
                FROM account_move_line aml
                JOIN account_move am ON am.id = aml.move_id
                WHERE aml.product_id IS NOT NULL
                  AND aml.display_type IS NULL
                  AND am.state = 'posted'
                  AND am.move_type IN ('out_invoice', 'out_refund')
                GROUP BY aml.product_id
            )
            SELECT
                ROW_NUMBER() OVER (ORDER BY lpm.source_db, lpm.source_product_id) AS id,
                lpm.source_db,
                lpm.source_product_id,
                lpm.source_default_code,
                lpm.source_barcode,
                lpm.source_name,
                lpm.source_category_name,
                lpm.source_brand_name,
                lpm.target_product_id,
                pp.product_tmpl_id AS target_product_tmpl_id,
                lpm.match_status,
                lpm.match_method,
                COALESCE(lpm.confidence, 0.0) AS confidence,
                COALESCE(ls.legacy_sales_qty, 0.0) AS legacy_sales_qty,
                COALESCE(ls.legacy_sales_amount, 0.0) AS legacy_sales_amount,
                COALESCE(cs.current_sales_qty, 0.0) AS current_sales_qty,
                COALESCE(cs.current_sales_amount, 0.0) AS current_sales_amount
            FROM legacy_product_map lpm
            LEFT JOIN legacy_sales ls ON ls.source_product_id = lpm.source_product_id
            LEFT JOIN current_sales cs ON cs.product_id = lpm.target_product_id
            LEFT JOIN product_product pp ON pp.id = lpm.target_product_id
            """
        )


class LegacyReportPackDefinition(models.Model):
    _name = "legacy.report.pack.definition"
    _description = "Legacy Report Pack Definition"
    _order = "sequence, id"

    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)

    name = fields.Char(required=True)
    code = fields.Char(required=True, index=True)
    report_xml_id = fields.Char(index=True)
    description = fields.Text()

    enabled = fields.Boolean(default=True)
    output_format = fields.Selection(
        selection=[("pdf", "PDF"), ("xlsx", "XLSX"), ("csv", "CSV"), ("html", "HTML")],
        default="pdf",
        required=True,
    )

    last_generated_at = fields.Datetime()
    last_generated_attachment_id = fields.Many2one("ir.attachment", ondelete="set null")

    payload_json = fields.Json()

    _sql_constraints = [
        ("legacy_report_pack_code_uniq", "unique(code)", "Report pack code must be unique."),
    ]

    def action_open_generate_wizard(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Generate Legacy Report",
            "res_model": "legacy.report.pack.generate.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_report_pack_id": self.id,
                "default_output_format": self.output_format or "csv",
            },
        }

    def action_open_last_generated_attachment(self):
        self.ensure_one()
        if not self.last_generated_attachment_id:
            raise UserError("No generated attachment is available for this report yet.")
        return {
            "type": "ir.actions.act_url",
            "url": f"/web/content/{self.last_generated_attachment_id.id}?download=1",
            "target": "self",
        }

    def action_open_interactive(self):
        self.ensure_one()
        code = self.code or ""
        invoice_codes = {
            "english_invoice",
            "invoice_selection",
            "invoice_standard",
            "invoice_with_payments",
        }
        payment_codes = {"payment_receipt"}
        serial_codes = {"lot_label", "inventory_report", "delivery_slip", "stock_picking"}

        if code in invoice_codes:
            action_xmlid = "legacy_invoice_archive.action_legacy_invoice_analysis"
        elif code in payment_codes:
            action_xmlid = "legacy_invoice_archive.action_legacy_payment_link_analysis"
        elif code in serial_codes:
            action_xmlid = "legacy_invoice_archive.action_legacy_serial_ref_analysis"
        else:
            action_xmlid = "legacy_invoice_archive.action_legacy_invoice_analysis"

        return self.env["ir.actions.act_window"]._for_xml_id(action_xmlid)

    def _get_invoice_domain(self, wizard):
        domain = [("active", "=", True)]
        if wizard.date_from:
            domain.append(("invoice_date", ">=", wizard.date_from))
        if wizard.date_to:
            domain.append(("invoice_date", "<=", wizard.date_to))
        if wizard.invoice_type != "all":
            domain.append(("invoice_type", "=", wizard.invoice_type))
        if wizard.invoice_state != "all":
            domain.append(("state", "=", wizard.invoice_state))
        return domain

    def _get_invoices(self, wizard):
        self.ensure_one()
        invoice_domain = self._get_invoice_domain(wizard)
        return self.env["legacy.invoice"].search(invoice_domain, order="invoice_date asc, id asc")

    def _is_invoice_style_code(self):
        self.ensure_one()
        return (self.code or "") in {
            "english_invoice",
            "invoice_selection",
            "invoice_standard",
            "invoice_with_payments",
        }

    def action_generate_report(self, wizard):
        self.ensure_one()
        if not self.enabled:
            raise UserError("This legacy report pack is disabled.")

        invoices = self._get_invoices(wizard)
        if not invoices:
            raise UserError("No legacy invoices matched the selected filters.")

        native_report = False
        if self.report_xml_id:
            native_report = self.env.ref(self.report_xml_id, raise_if_not_found=False)
        if native_report and native_report._name == "ir.actions.report":
            if native_report.model == "legacy.invoice":
                return native_report.report_action(invoices)
            if native_report.model == "legacy.report.pack.generate.wizard":
                return native_report.report_action(wizard)

        if self._is_invoice_style_code():
            invoice_report_xml_id = (
                "legacy_invoice_archive.action_report_legacy_invoice_html"
                if wizard.output_format == "html"
                else "legacy_invoice_archive.action_report_legacy_invoice"
            )
            invoice_report = self.env.ref(invoice_report_xml_id, raise_if_not_found=False)
            if not invoice_report:
                raise UserError("Legacy invoice report action is missing.")
            return invoice_report.report_action(invoices)

        preview_xml_id = (
            "legacy_invoice_archive.action_report_legacy_report_pack_preview_html"
            if wizard.output_format == "html"
            else "legacy_invoice_archive.action_report_legacy_report_pack_preview_pdf"
        )
        preview_report = self.env.ref(preview_xml_id, raise_if_not_found=False)
        if not preview_report:
            raise UserError("Legacy report preview action is missing.")
        return preview_report.report_action(wizard)

    def _build_report_rows(self, invoices):
        self.ensure_one()
        code = self.code or ""

        if code in {"english_invoice", "invoice_selection", "invoice_standard", "invoice_with_payments"}:
            headers = [
                "invoice_date",
                "number",
                "source_name",
                "customer",
                "state",
                "invoice_type",
                "amount_untaxed",
                "amount_tax",
                "amount_total",
                "amount_residual",
                "payment_amount",
                "payment_methods",
            ]
            rows = []
            for inv in invoices:
                payment_amount = sum(inv.payment_link_ids.mapped("amount")) if code == "invoice_with_payments" else 0.0
                payment_methods = ", ".join(
                    sorted(
                        {
                            p.payment_method_name or p.payment_method_code or ""
                            for p in inv.payment_link_ids
                            if (p.payment_method_name or p.payment_method_code)
                        }
                    )
                )
                rows.append(
                    {
                        "invoice_date": inv.invoice_date or "",
                        "number": inv.number or "",
                        "source_name": inv.source_name or "",
                        "customer": inv.partner_id.display_name or inv.source_partner_name or "",
                        "state": inv.state or "",
                        "invoice_type": inv.invoice_type or "",
                        "amount_untaxed": inv.amount_untaxed or 0.0,
                        "amount_tax": inv.amount_tax or 0.0,
                        "amount_total": inv.amount_total or 0.0,
                        "amount_residual": inv.amount_residual or 0.0,
                        "payment_amount": payment_amount,
                        "payment_methods": payment_methods,
                    }
                )
            return headers, rows

        if code == "payment_receipt":
            headers = [
                "invoice_date",
                "invoice_number",
                "customer",
                "payment_date",
                "payment_name",
                "reference",
                "journal",
                "payment_method",
                "amount",
            ]
            rows = []
            payment_links = self.env["legacy.invoice.payment.link"].search([("invoice_id", "in", invoices.ids)])
            for payment in payment_links:
                inv = payment.invoice_id
                rows.append(
                    {
                        "invoice_date": inv.invoice_date or "",
                        "invoice_number": inv.number or "",
                        "customer": inv.partner_id.display_name or inv.source_partner_name or "",
                        "payment_date": payment.payment_date or "",
                        "payment_name": payment.name or "",
                        "reference": payment.reference or "",
                        "journal": payment.journal_id.display_name or payment.journal_name or payment.journal_code or "",
                        "payment_method": payment.payment_method_name or payment.payment_method_code or "",
                        "amount": payment.amount or 0.0,
                    }
                )
            return headers, rows

        # lot/inventory/delivery/picking legacy reports use the serial reference dataset.
        headers = [
            "invoice_date",
            "invoice_number",
            "customer",
            "lot_name",
            "item_code",
            "qty_done",
            "product_category",
            "picking_note",
            "match_status",
        ]
        rows = []
        serial_refs = self.env["legacy.invoice.serial.ref"].search([("invoice_id", "in", invoices.ids)])
        for serial in serial_refs:
            inv = serial.invoice_id
            rows.append(
                {
                    "invoice_date": inv.invoice_date or "",
                    "invoice_number": inv.number or "",
                    "customer": inv.partner_id.display_name or inv.source_partner_name or "",
                    "lot_name": serial.lot_name or "",
                    "item_code": serial.item_code or "",
                    "qty_done": serial.qty_done or 0.0,
                    "product_category": serial.product_category_id.display_name or "",
                    "picking_note": serial.picking_note or "",
                    "match_status": serial.match_status or "",
                }
            )
        return headers, rows

    def _render_report_content(self, headers, rows, output_format):
        # For phase-1 legacy reporting regeneration we provide CSV/HTML dataset outputs.
        # PDF/XLSX requests fallback to CSV to avoid pretending unsupported renderers.
        if output_format == "html":
            parts = [
                "<html><body><table border='1' cellspacing='0' cellpadding='4'>",
                "<thead><tr>",
            ]
            for header in headers:
                parts.append(f"<th>{html.escape(str(header))}</th>")
            parts.append("</tr></thead><tbody>")
            for row in rows:
                parts.append("<tr>")
                for header in headers:
                    value = row.get(header, "")
                    parts.append(f"<td>{html.escape(str(value))}</td>")
                parts.append("</tr>")
            parts.append("</tbody></table></body></html>")
            return "".join(parts).encode("utf-8"), "text/html", "html"

        stream = io.StringIO()
        writer = csv.DictWriter(stream, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        return stream.getvalue().encode("utf-8-sig"), "text/csv", "csv"

    def generate_report_attachment(self, wizard):
        self.ensure_one()
        if not self.enabled:
            raise UserError("This legacy report pack is disabled.")

        invoices = self._get_invoices(wizard)
        headers, rows = self._build_report_rows(invoices)
        content, mimetype, extension = self._render_report_content(headers, rows, wizard.output_format)

        stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        filename = f"legacy_{self.code}_{stamp}.{extension}"
        attachment = self.env["ir.attachment"].create(
            {
                "name": filename,
                "datas_fname": filename,
                "type": "binary",
                "datas": base64.b64encode(content).decode(),
                "mimetype": mimetype,
                "res_model": self._name,
                "res_id": self.id,
            }
        )
        self.write(
            {
                "last_generated_at": fields.Datetime.now(),
                "last_generated_attachment_id": attachment.id,
            }
        )
        return attachment

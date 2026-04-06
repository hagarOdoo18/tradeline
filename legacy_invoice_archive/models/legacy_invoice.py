from odoo import api, fields, models


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

    number = fields.Char(index=True)
    source_name = fields.Char(index=True)
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


class LegacyInvoiceLine(models.Model):
    _name = "legacy.invoice.line"
    _description = "Legacy Invoice Line"
    _order = "invoice_id, sequence, id"

    invoice_id = fields.Many2one("legacy.invoice", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="invoice_id.company_id", store=True, index=True)
    currency_id = fields.Many2one(related="invoice_id.currency_id", store=True)

    source_id = fields.Integer(required=True, index=True)
    source_move_line_id = fields.Integer(index=True)

    sequence = fields.Integer(default=10)
    name = fields.Text()

    product_id = fields.Many2one("product.product", ondelete="set null")
    product_tmpl_id = fields.Many2one("product.template", ondelete="set null")
    item_code = fields.Char(index=True)

    quantity = fields.Float()
    uom_id = fields.Many2one("uom.uom", ondelete="set null")
    price_unit = fields.Monetary(currency_field="currency_id")
    discount = fields.Float()
    price_subtotal = fields.Monetary(currency_field="currency_id")
    price_total = fields.Monetary(currency_field="currency_id")

    account_id = fields.Many2one("account.account", ondelete="set null")

    legacy_payload = fields.Json()

    _sql_constraints = [
        (
            "legacy_invoice_line_source_uniq",
            "unique(invoice_id, source_id)",
            "The source line identity must be unique per invoice.",
        ),
    ]


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

    payment_id = fields.Many2one("account.payment", ondelete="set null")
    journal_id = fields.Many2one("account.journal", ondelete="set null")

    name = fields.Char()
    reference = fields.Char(index=True)
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

    lot_name = fields.Char(index=True)
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
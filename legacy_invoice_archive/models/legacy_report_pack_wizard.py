from odoo import fields, models
from odoo.exceptions import UserError


class LegacyReportPackGenerateWizard(models.TransientModel):
    _name = "legacy.report.pack.generate.wizard"
    _description = "Legacy Report Pack Generate Wizard"

    report_pack_id = fields.Many2one("legacy.report.pack.definition", required=True, readonly=True)
    date_from = fields.Date()
    date_to = fields.Date()

    invoice_type = fields.Selection(
        selection=[
            ("all", "All Types"),
            ("out_invoice", "Customer Invoice"),
            ("out_refund", "Credit Note"),
            ("in_invoice", "Vendor Bill"),
            ("in_refund", "Vendor Credit Note"),
            ("other", "Other"),
        ],
        default="all",
        required=True,
    )
    invoice_state = fields.Selection(
        selection=[
            ("all", "All States"),
            ("draft", "Draft"),
            ("open", "Open"),
            ("paid", "Paid"),
            ("cancel", "Cancelled"),
            ("proforma", "Pro-Forma"),
            ("proforma2", "Pro-Forma 2"),
            ("other", "Other"),
        ],
        default="all",
        required=True,
    )
    output_format = fields.Selection(
        selection=[("csv", "CSV"), ("html", "HTML"), ("xlsx", "XLSX (CSV fallback)"), ("pdf", "PDF")],
        default="csv",
        required=True,
    )

    def get_preview_payload(self):
        self.ensure_one()
        row_limit = 2000
        try:
            row_limit = int(
                self.env["ir.config_parameter"].sudo().get_param(
                    "legacy_invoice_archive.report_preview_row_limit",
                    row_limit,
                )
            )
        except Exception:
            row_limit = 2000
        row_limit = max(1, min(row_limit, 10000))
        report_pack = self.report_pack_id
        invoice_model = self.env["legacy.invoice"]
        invoice_domain = report_pack._get_invoice_domain(self)
        invoice_count = invoice_model.search_count(invoice_domain)
        if report_pack._is_invoice_style_code():
            invoices = invoice_model.search(invoice_domain, order="invoice_date asc, id asc", limit=row_limit)
            headers, rows = report_pack._build_report_rows(invoices)
            total_rows = invoice_count
        else:
            # Filter through the invoice relation instead of materializing every
            # invoice ID. Limit child rows before reading their fields.
            row_model_name = ("legacy.invoice.payment.link" if report_pack.code == "payment_receipt"
                              else "legacy.invoice.serial.ref")
            row_model = self.env[row_model_name]
            row_domain = [("invoice_id." + field, operator, value) for field, operator, value in invoice_domain]
            records = row_model.search(row_domain, limit=row_limit)
            total_rows = row_model.search_count(row_domain)
            rows_kw = {"payment_links" if report_pack.code == "payment_receipt" else "serial_refs": records}
            headers, rows = report_pack._build_report_rows(invoice_model.browse(), **rows_kw)
        truncated = total_rows > row_limit
        return {
            "invoice_count": invoice_count,
            "headers": headers,
            "rows": rows,
            "total_rows": total_rows,
            "truncated": truncated,
            "row_limit": row_limit,
        }

    def action_generate(self):
        self.ensure_one()
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise UserError("'Date From' cannot be later than 'Date To'.")
        if self.output_format in {"pdf", "html"}:
            return self.report_pack_id.action_generate_report(self)
        attachment = self.report_pack_id.generate_report_attachment(self)
        return {
            "type": "ir.actions.act_url",
            "url": f"/web/content/{attachment.id}?download=1",
            "target": "self",
        }

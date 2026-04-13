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
        invoices = self.report_pack_id._get_invoices(self)
        headers, rows = self.report_pack_id._build_report_rows(invoices)
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
        total_rows = len(rows)
        truncated = total_rows > row_limit
        preview_rows = rows[:row_limit] if truncated else rows
        return {
            "invoice_count": len(invoices),
            "headers": headers,
            "rows": preview_rows,
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

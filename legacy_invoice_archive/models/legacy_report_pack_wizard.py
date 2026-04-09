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
        selection=[("csv", "CSV"), ("html", "HTML"), ("xlsx", "XLSX (CSV fallback)"), ("pdf", "PDF (CSV fallback)")],
        default="csv",
        required=True,
    )

    def action_generate(self):
        self.ensure_one()
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise UserError("'Date From' cannot be later than 'Date To'.")
        attachment = self.report_pack_id.generate_report_attachment(self)
        return {
            "type": "ir.actions.act_url",
            "url": f"/web/content/{attachment.id}?download=1",
            "target": "self",
        }

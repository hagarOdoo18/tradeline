from odoo import fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    legacy_invoice_count = fields.Integer(compute="_compute_legacy_invoice_count", string="Legacy Invoices")

    def _compute_legacy_invoice_count(self):
        counts = {}
        grouped = self.env["legacy.invoice"].read_group(
            domain=[("partner_id", "in", self.ids)],
            fields=["partner_id"],
            groupby=["partner_id"],
            lazy=False,
        )
        for item in grouped:
            partner_id = item.get("partner_id")
            if partner_id:
                counts[partner_id[0]] = item.get("partner_id_count", 0)

        for partner in self:
            partner.legacy_invoice_count = counts.get(partner.id, 0)

    def action_open_legacy_invoices(self):
        self.ensure_one()
        action = self.env.ref("legacy_invoice_archive.action_legacy_invoice").read()[0]
        action["domain"] = [("partner_id", "=", self.id)]
        action["context"] = dict(self.env.context, default_partner_id=self.id)
        return action
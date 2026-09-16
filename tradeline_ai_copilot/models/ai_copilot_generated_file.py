from odoo import fields, models


class AICopilotGeneratedFile(models.Model):
    _name = "ai.copilot.generated.file"
    _description = "AI Copilot Generated File"
    _order = "id desc"

    name = fields.Char(required=True)
    file_type = fields.Selection([("csv", "CSV"), ("xlsx", "XLSX"), ("pdf", "PDF")], required=True)
    attachment_id = fields.Many2one("ir.attachment", required=True, ondelete="cascade")
    user_id = fields.Many2one("res.users", required=True, default=lambda self: self.env.user, index=True)
    conversation_id = fields.Many2one("ai.copilot.conversation", ondelete="set null", index=True)
    source_model = fields.Char()
    row_count = fields.Integer(default=0)
    metadata_json = fields.Json()
    download_url = fields.Char(compute="_compute_download_url")

    def _compute_download_url(self):
        for record in self:
            if record.attachment_id:
                record.download_url = "/web/content/%s?download=1" % record.attachment_id.id
            else:
                record.download_url = False


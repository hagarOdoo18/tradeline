from odoo import fields, models


class AICopilotAuditLog(models.Model):
    _name = "ai.copilot.audit.log"
    _description = "AI Copilot Audit Log"
    _order = "id desc"

    user_id = fields.Many2one("res.users", required=True, default=lambda self: self.env.user, index=True)
    conversation_id = fields.Many2one("ai.copilot.conversation", ondelete="set null", index=True)
    prompt = fields.Text()
    intent = fields.Char()
    model_accessed = fields.Char()
    fields_accessed = fields.Text()
    domain_json = fields.Text()
    row_count = fields.Integer(default=0)
    duration_ms = fields.Integer(default=0)
    provider = fields.Selection([("openai", "OpenAI"), ("claude", "Claude")])
    llm_model = fields.Char()
    status = fields.Selection([("ok", "OK"), ("error", "Error")], default="ok", required=True)
    error_message = fields.Text()
    file_generated = fields.Boolean(default=False)


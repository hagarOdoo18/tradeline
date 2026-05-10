from odoo import fields, models


class AICopilotMessage(models.Model):
    _name = "ai.copilot.message"
    _description = "AI Copilot Message"
    _order = "id asc"

    conversation_id = fields.Many2one("ai.copilot.conversation", required=True, ondelete="cascade", index=True)
    user_id = fields.Many2one(related="conversation_id.user_id", store=True, index=True)
    role = fields.Selection(
        [("user", "User"), ("assistant", "Assistant"), ("system", "System"), ("tool", "Tool")],
        default="user",
        required=True,
    )
    content = fields.Text(required=True)
    response_json = fields.Json()
    provider = fields.Selection([("openai", "OpenAI"), ("claude", "Claude")])
    llm_model = fields.Char()
    token_count = fields.Integer(default=0)
    tool_plan_json = fields.Json()


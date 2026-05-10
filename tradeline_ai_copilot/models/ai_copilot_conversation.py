from odoo import fields, models


class AICopilotConversation(models.Model):
    _name = "ai.copilot.conversation"
    _description = "AI Copilot Conversation"
    _order = "write_date desc, id desc"

    name = fields.Char(required=True, default="New Conversation")
    user_id = fields.Many2one("res.users", required=True, default=lambda self: self.env.user, index=True)
    provider = fields.Selection([("openai", "OpenAI"), ("claude", "Claude")], default="openai")
    llm_model = fields.Char()
    mode = fields.Selection(
        [("compact", "Compact"), ("side", "Side Panel"), ("fullscreen", "Fullscreen")],
        default="compact",
    )
    context_model = fields.Char()
    context_record_id = fields.Integer()
    context_payload = fields.Json()
    pinned = fields.Boolean(default=False)
    archived = fields.Boolean(default=False)
    message_ids = fields.One2many("ai.copilot.message", "conversation_id")
    generated_file_ids = fields.One2many("ai.copilot.generated.file", "conversation_id")
    message_count = fields.Integer(compute="_compute_message_count")

    def _compute_message_count(self):
        grouped = self.env["ai.copilot.message"].read_group(
            [("conversation_id", "in", self.ids)],
            ["conversation_id"],
            ["conversation_id"],
        )
        count_map = {item["conversation_id"][0]: item["conversation_id_count"] for item in grouped if item.get("conversation_id")}
        for record in self:
            record.message_count = count_map.get(record.id, 0)


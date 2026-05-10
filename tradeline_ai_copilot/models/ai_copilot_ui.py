from odoo import api, models
from odoo.exceptions import AccessError, UserError


class AICopilotUI(models.AbstractModel):
    _name = "ai.copilot.ui"
    _description = "AI Copilot UI Bridge"

    def _assert_internal_user(self):
        if not self.env.user.has_group("base.group_user"):
            raise AccessError("Only internal Odoo users can access the AI copilot.")

    def _get_conversation(self, conversation_id=None):
        conversation_env = self.env["ai.copilot.conversation"]
        if conversation_id:
            conversation = conversation_env.browse(int(conversation_id))
            if not conversation.exists():
                raise UserError("Conversation not found.")
            if conversation.user_id != self.env.user and not self.env.user.has_group("base.group_system"):
                raise AccessError("You can only access your own conversation.")
            return conversation
        return conversation_env.create(
            {
                "name": "Conversation - %s" % self.env.user.name,
                "user_id": self.env.user.id,
            }
        )

    @api.model
    def chat_send(self, payload):
        self._assert_internal_user()
        payload = payload or {}
        message = payload.get("message")
        if not message:
            raise UserError("Message is required.")

        conversation = self._get_conversation(payload.get("conversation_id"))
        provider = payload.get("provider")
        llm_model = payload.get("model")
        context_payload = payload.get("context") or {}

        message_env = self.env["ai.copilot.message"]
        message_env.create(
            {
                "conversation_id": conversation.id,
                "role": "user",
                "content": message,
                "provider": provider,
                "llm_model": llm_model,
            }
        )

        response = self.env["ai.copilot.service"].generate_response(
            message,
            conversation=conversation,
            context_payload=context_payload,
            provider=provider,
            llm_model=llm_model,
        )
        message_env.create(
            {
                "conversation_id": conversation.id,
                "role": "assistant",
                "content": response["blocks"][0]["content"] if response.get("blocks") else "",
                "response_json": response.get("blocks", []),
                "provider": response.get("provider"),
                "llm_model": response.get("llm_model"),
            }
        )
        return {
            "conversation_id": conversation.id,
            "provider": response.get("provider"),
            "model": response.get("llm_model"),
            "blocks": response.get("blocks", []),
            "query_meta": response.get("query_meta"),
            "file_ids": response.get("file_ids", []),
        }

    @api.model
    def export_file(self, payload):
        self._assert_internal_user()
        payload = payload or {}
        file_type = payload.get("file_type")
        query_meta = payload.get("query_meta")
        conversation_id = payload.get("conversation_id")
        if file_type not in {"csv", "xlsx"}:
            raise UserError("Unsupported export type.")
        if not query_meta:
            raise UserError("query_meta is required.")
        return self.env["ai.copilot.service"].export_from_query_meta(
            query_meta,
            file_type,
            conversation_id=conversation_id,
        )


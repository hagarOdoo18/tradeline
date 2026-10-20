from odoo import api, models
from odoo.exceptions import AccessError, UserError


class AICopilotUI(models.AbstractModel):
    _name = "ai.copilot.ui"
    _description = "AI Copilot UI Bridge"

    def _assert_internal_user(self):
        self.env["ai.copilot.service"]._assert_internal_user()

    def _get_conversation(self, conversation_id=None):
        conversation_env = self.env["ai.copilot.conversation"]
        if conversation_id:
            conversation = conversation_env.browse(int(conversation_id))
            if not conversation.exists():
                raise UserError("Conversation not found.")
            if conversation.user_id != self.env.user:
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
        if not isinstance(message, str) or len(message) > 12000:
            raise UserError("Keep your question under 12,000 characters.")

        conversation = self._get_conversation(payload.get("conversation_id"))
        if not conversation.message_ids:
            conversation.name = message[:80]
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
        assistant_message = message_env.create(
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
            "message_id": assistant_message.id,
            "conversation_id": conversation.id,
            "provider": response.get("provider"),
            "model": response.get("llm_model"),
            "blocks": response.get("blocks", []),
            "query_meta": response.get("query_meta"),
            "file_ids": response.get("file_ids", []),
        }

    @api.model
    def bootstrap(self):
        self._assert_internal_user()
        settings = self.env["ai.copilot.service"].get_settings()
        return {"provider": settings.default_provider, "models": {
            "claude": settings.default_claude_model, "openai": settings.default_openai_model},
            "enable_csv": settings.enable_csv, "enable_xlsx": settings.enable_xlsx,
            "enable_history": settings.enable_history}

    def _chat_stream(self, payload):
        """Run only inside the streaming controller's independently owned cursor."""
        self._assert_internal_user()
        if not isinstance(payload, dict):
            raise UserError("Invalid chat request.")
        message = payload.get("message")
        if not isinstance(message, str) or not message.strip() or len(message) > 12000:
            raise UserError("Keep your question between 1 and 12,000 characters.")
        conversation = self._get_conversation(payload.get("conversation_id"))
        if not conversation.message_ids:
            conversation.name = message[:80]
        self.env["ai.copilot.message"].create({"conversation_id": conversation.id, "role": "user", "content": message})
        yield {"type": "started", "conversation_id": conversation.id}
        events = self.env["ai.copilot.service"]._generate_response_stream(
            message, conversation=conversation, provider=payload.get("provider"))
        try:
            for event in events:
                if event["type"] != "response":
                    yield event
                    continue
                response = event["response"]
                saved = self.env["ai.copilot.message"].create({
                    "conversation_id": conversation.id, "role": "assistant",
                    "content": response["blocks"][0]["content"], "response_json": response["blocks"],
                    "provider": response.get("provider"), "llm_model": response.get("llm_model")})
                yield {"type": "complete", "response": {"conversation_id": conversation.id,
                    "message_id": saved.id, "provider": response.get("provider"), "blocks": response["blocks"]}}
        finally:
            events.close()

    @api.model
    def history(self):
        self._assert_internal_user()
        if not self.env["ai.copilot.service"].get_settings().enable_history:
            return []
        records = self.env["ai.copilot.conversation"].search([
            ("user_id", "=", self.env.user.id), ("archived", "=", False)], limit=80)
        return [{"id": record.id, "name": record.name, "provider": record.provider,
                 "updated": record.write_date} for record in records]

    @api.model
    def load_conversation(self, conversation_id):
        self._assert_internal_user()
        if not conversation_id:
            raise UserError("Choose a conversation to load.")
        conversation = self._get_conversation(conversation_id)
        messages = self.env["ai.copilot.message"].search([
            ("conversation_id", "=", conversation.id)], order="id desc", limit=100)
        return {"id": conversation.id, "provider": conversation.provider,
                "messages": [{"id": msg.id, "role": msg.role, "content": msg.content,
                              "blocks": msg.response_json or []} for msg in reversed(messages)]}

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


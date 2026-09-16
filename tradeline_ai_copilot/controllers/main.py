from odoo import http
from odoo.exceptions import AccessError, UserError
from odoo.http import request


class AICopilotController(http.Controller):
    def _assert_internal(self):
        user = request.env.user
        if not user.has_group("base.group_user"):
            raise AccessError("Only internal Odoo users can access this endpoint.")

    def _get_conversation(self, conversation_id=None):
        conversation_env = request.env["ai.copilot.conversation"]
        if conversation_id:
            conversation = conversation_env.browse(int(conversation_id))
            if not conversation.exists():
                raise UserError("Conversation not found.")
            if conversation.user_id != request.env.user and not request.env.user.has_group("base.group_system"):
                raise AccessError("You can only access your own conversation.")
            return conversation
        return conversation_env.create(
            {
                "name": "Conversation - %s" % request.env.user.name,
                "user_id": request.env.user.id,
            }
        )

    @http.route("/ai_copilot/chat/send", type="json", auth="user")
    def chat_send(self, message=None, conversation_id=None, context=None, provider=None, model=None, **kwargs):
        self._assert_internal()
        if not message:
            raise UserError("Message is required.")

        conversation = self._get_conversation(conversation_id)
        message_env = request.env["ai.copilot.message"]
        message_env.create(
            {
                "conversation_id": conversation.id,
                "role": "user",
                "content": message,
                "provider": provider,
                "llm_model": model,
            }
        )

        response = request.env["ai.copilot.service"].generate_response(
            message,
            conversation=conversation,
            context_payload=context or {},
            provider=provider,
            llm_model=model,
        )
        message_env.create(
            {
                "conversation_id": conversation.id,
                "role": "assistant",
                "content": response["blocks"][0]["content"] if response.get("blocks") else "",
                "response_json": response["blocks"],
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

    @http.route("/ai_copilot/chat/history", type="json", auth="user")
    def chat_history(self, limit=20, offset=0, **kwargs):
        self._assert_internal()
        conversation_env = request.env["ai.copilot.conversation"]
        domain = []
        if not request.env.user.has_group("base.group_system"):
            domain = [("user_id", "=", request.env.user.id)]
        records = conversation_env.search(domain, limit=int(limit), offset=int(offset), order="write_date desc,id desc")
        return [
            {
                "id": record.id,
                "name": record.name,
                "user_id": record.user_id.id,
                "user_name": record.user_id.name,
                "provider": record.provider,
                "model": record.llm_model,
                "message_count": record.message_count,
                "write_date": record.write_date,
            }
            for record in records
        ]

    @http.route("/ai_copilot/chat/conversation", type="json", auth="user")
    def chat_conversation(self, conversation_id=None, **kwargs):
        self._assert_internal()
        if not conversation_id:
            raise UserError("conversation_id is required.")
        conversation = self._get_conversation(conversation_id)
        messages = request.env["ai.copilot.message"].search([("conversation_id", "=", conversation.id)], order="id asc")
        return {
            "id": conversation.id,
            "name": conversation.name,
            "messages": [
                {
                    "id": msg.id,
                    "role": msg.role,
                    "content": msg.content,
                    "response_json": msg.response_json or [],
                    "created": msg.create_date,
                }
                for msg in messages
            ],
        }

    @http.route("/ai_copilot/export/csv", type="json", auth="user")
    def export_csv(self, query_meta=None, conversation_id=None, **kwargs):
        self._assert_internal()
        if not query_meta:
            raise UserError("query_meta is required.")
        return request.env["ai.copilot.service"].export_from_query_meta(query_meta, "csv", conversation_id=conversation_id)

    @http.route("/ai_copilot/export/xlsx", type="json", auth="user")
    def export_xlsx(self, query_meta=None, conversation_id=None, **kwargs):
        self._assert_internal()
        if not query_meta:
            raise UserError("query_meta is required.")
        return request.env["ai.copilot.service"].export_from_query_meta(query_meta, "xlsx", conversation_id=conversation_id)

    @http.route("/ai_copilot/settings/test_provider", type="json", auth="user")
    def test_provider(self, provider=None, **kwargs):
        self._assert_internal()
        if not request.env.user.has_group("base.group_system"):
            raise AccessError("Only admins can test provider configuration.")
        settings = request.env["ai.copilot.settings"].sudo().get_singleton()
        provider = provider or settings.default_provider
        if provider == "openai":
            configured = bool(settings.openai_api_key and settings.default_openai_model)
        elif provider == "claude":
            configured = bool(settings.claude_api_key and settings.default_claude_model)
        else:
            configured = False
        return {"provider": provider, "configured": configured}

    @http.route("/ai_copilot/settings/refresh_policy", type="json", auth="user")
    def refresh_policy(self, **kwargs):
        self._assert_internal()
        if not request.env.user.has_group("base.group_system"):
            raise AccessError("Only admins can refresh policy.")
        request.env["ai.copilot.service"].refresh_allowed_models()
        return {"ok": True}


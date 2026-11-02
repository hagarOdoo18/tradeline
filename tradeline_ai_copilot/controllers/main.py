import json
import logging
from contextlib import closing

from odoo import api, http
from odoo.exceptions import AccessError, UserError
from odoo.http import request

_logger = logging.getLogger(__name__)


class AICopilotController(http.Controller):
    @staticmethod
    def _encode_event(event):
        return "data: " + json.dumps(event, ensure_ascii=False, default=str) + "\n\n"

    @http.route("/ai_copilot/chat/stream", type="http", auth="user", methods=["POST"], csrf=True)
    def chat_stream(self, payload=None, **kwargs):
        self._assert_internal()
        if not isinstance(payload, str) or len(payload) > 20000:
            return request.make_response("Invalid chat request", status=400)
        try:
            values = json.loads(payload)
            if not isinstance(values, dict):
                raise ValueError()
            # Use only the active companies explicitly supplied by the web client's user context.
            # No form data, arbitrary Odoo context flags, or unsaved values are accepted.
            companies = values.get("company_ids") or request.env.companies.ids
            if not isinstance(companies, list) or not companies or any(type(i) is not int for i in companies):
                raise ValueError()
            if not set(companies) <= set(request.env.user.company_ids.ids):
                raise AccessError("You cannot query an unavailable company.")
        except (ValueError, TypeError):
            return request.make_response("Invalid chat request", status=400)
        registry, uid = request.env.registry, request.env.uid
        context = dict(request.env.context, allowed_company_ids=companies)
        # Never hold request.env inside the iterable: Odoo closes its HTTP cursor
        # before consuming a streaming body. This transaction belongs solely to the chat.
        def generate():
            try:
                with registry.cursor() as cr:
                    env = api.Environment(cr, uid, context)
                    with closing(env["ai.copilot.ui"]._chat_stream(values)) as events:
                        for event in events:
                            if event["type"] == "complete":
                                cr.commit()
                                yield self._encode_event({"type": "done", **event["response"]})
                            else:
                                yield self._encode_event(event)
            except (AccessError, UserError) as exc:
                yield self._encode_event({"type": "error", "message": str(exc)[:500]})
            except Exception:
                _logger.warning("Copilot streaming request failed; no request contents logged")
                yield self._encode_event({"type": "error", "message": "The report could not be completed. Please try again."})
        return request.make_response(generate(), headers=[
            ("Content-Type", "text/event-stream; charset=utf-8"),
            ("Cache-Control", "no-cache, no-store"), ("X-Accel-Buffering", "no")])

    def _assert_internal(self):
        request.env["ai.copilot.service"]._assert_internal_user()

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
        return request.env["ai.copilot.ui"].chat_send({
            "message": message, "conversation_id": conversation_id,
            "context": context, "provider": provider, "model": model})

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
        if not request.env.user.has_group("base.group_system"):
            raise AccessError("Only admins can refresh policy.")
        request.env["ai.copilot.service"].refresh_allowed_models()
        return {"ok": True}


from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged
from unittest.mock import patch


@tagged("post_install", "-at_install")
class TestAICopilotSecurity(TransactionCase):
    def setUp(self):
        super().setUp()
        group_user = self.env.ref("tradeline_ai_copilot.group_ai_copilot_user")
        self.internal_user = self.env["res.users"].with_context(no_reset_password=True).create(
            {
                "name": "AI Copilot User",
                "login": "ai_copilot_user",
                "email": "ai_copilot_user@example.com",
                "groups_id": [(6, 0, [group_user.id])],
            }
        )
        self.env["ai.copilot.settings"].sudo().get_singleton()

    def test_internal_user_can_start_conversation(self):
        conversation = self.env["ai.copilot.conversation"].with_user(self.internal_user).create(
            {"name": "Test Conversation", "user_id": self.internal_user.id}
        )
        self.assertTrue(conversation.exists())
        self.assertEqual(conversation.user_id, self.internal_user)

    def test_non_admin_cannot_refresh_policy(self):
        service = self.env["ai.copilot.service"].with_user(self.internal_user)
        with self.assertRaises(AccessError):
            service.refresh_allowed_models()

    def test_technical_model_is_denied(self):
        service = self.env["ai.copilot.service"].with_user(self.internal_user)
        with self.assertRaises(AccessError):
            service._check_model_access("ir.model")

    def _remove_reporting_access(self):
        self.internal_user.write({"groups_id": [(3, self.env.ref("tradeline_ai_copilot.group_ai_copilot_user").id)]})
        self.assertTrue(self.internal_user.has_group("base.group_user"))

    def test_unapproved_internal_user_cannot_call_reporting_endpoints(self):
        self._remove_reporting_access()
        ui = self.env["ai.copilot.ui"].with_user(self.internal_user)
        service = self.env["ai.copilot.service"].with_user(self.internal_user)
        query = self.env["ai.copilot.query"].with_user(self.internal_user)
        calls = [ui.bootstrap, ui.history, lambda: ui.chat_send({"message": "Sales"}),
                 lambda: list(ui._chat_stream({"message": "Sales"})),
                 lambda: service.generate_response("Sales"),
                 lambda: list(service._generate_response_stream("Sales")),
                 lambda: query.search_models("sale"), lambda: query.describe_model("res.partner"),
                 lambda: query.execute_query({"model": "res.partner", "domain": []}),
                 lambda: service.export_from_query_meta({"model": "res.partner", "domain": []}, "xlsx")]
        with patch("odoo.addons.tradeline_ai_copilot.models.ai_copilot_agent.requests.post") as post:
            for call in calls:
                with self.subTest(call=call), self.assertRaises(AccessError):
                    call()
            post.assert_not_called()
        # Removing assistant access must not remove ordinary ERP access.
        self.assertTrue(self.internal_user.partner_id.with_user(self.internal_user).read(["name"]))

    def test_unapproved_internal_user_has_no_copilot_model_acl(self):
        self._remove_reporting_access()
        for model in ["ai.copilot.conversation", "ai.copilot.message", "ai.copilot.generated.file", "ai.copilot.audit.log"]:
            with self.subTest(model=model), self.assertRaises(AccessError):
                self.env[model].with_user(self.internal_user).check_access_rights("read")
        with self.assertRaises(AccessError):
            self.env["ai.copilot.conversation"].with_user(self.internal_user).create({"name": "Denied"})

    def test_group_member_cannot_read_provider_keys_or_change_settings(self):
        self.env["ai.copilot.ui"].with_user(self.internal_user).bootstrap()
        for model in ["ai.copilot.settings", "ai.copilot.allowed.model"]:
            with self.subTest(model=model), self.assertRaises(AccessError):
                self.env[model].with_user(self.internal_user).check_access_rights("read")

    def test_revoked_member_cannot_read_existing_conversation(self):
        conversation = self.env["ai.copilot.conversation"].with_user(self.internal_user).create({"name": "Before revocation"})
        self._remove_reporting_access()
        with self.assertRaises(AccessError):
            self.env["ai.copilot.ui"].with_user(self.internal_user).load_conversation(conversation.id)
        with self.assertRaises(AccessError):
            conversation.with_user(self.internal_user).read(["name"])

    def test_chat_menus_replace_old_internal_user_access(self):
        group = self.env.ref("tradeline_ai_copilot.group_ai_copilot_user")
        for name in ["workspace", "conversations", "generated_files"]:
            menu = self.env.ref("tradeline_ai_copilot.menu_tradeline_ai_" + name)
            self.assertEqual(menu.groups_id, group)
        for name in ["conversation", "message", "generated_file", "audit_log"]:
            rule = self.env.ref("tradeline_ai_copilot.rule_ai_copilot_" + name + "_own")
            self.assertEqual(rule.groups, group)

    def test_administrator_can_manage_without_reporting_access(self):
        self.internal_user.write({"groups_id": [(6, 0, self.env.ref("base.group_system").ids)]})
        service = self.env["ai.copilot.service"].with_user(self.internal_user)
        self.assertTrue(service.refresh_allowed_models())
        self.assertTrue(service.get_settings())
        with self.assertRaises(AccessError):
            service.generate_response("Sales")

    def test_builtin_administrator_can_use_reporting(self):
        administrator = self.env.ref("base.user_admin")
        self.assertTrue(administrator.has_group("tradeline_ai_copilot.group_ai_copilot_user"))
        config = self.env["ai.copilot.ui"].with_user(administrator).bootstrap()
        self.assertIn(config["provider"], {"claude", "openai"})


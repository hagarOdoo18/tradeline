from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestAICopilotSecurity(TransactionCase):
    def setUp(self):
        super().setUp()
        group_user = self.env.ref("base.group_user")
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


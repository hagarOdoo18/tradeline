from odoo.exceptions import AccessError, ValidationError
from types import SimpleNamespace
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestMCPReporting(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.reader = cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": "MCP isolated QA reader", "login": "mcp_isolated_qa_reader",
            "company_id": cls.env.company.id, "company_ids": [(6, 0, cls.env.company.ids)],
            "groups_id": [(6, 0, cls.env.ref("tradeline_mcp_reporting.group_mcp_reporting_user").ids)]})
        cls.settings = cls.env["ai.copilot.settings"].sudo().get_singleton()
        cls.settings.enable_mcp_reporting = True
        cls.bridge = cls.env["tradeline.mcp.reporting"].with_user(cls.reader)
        cls.policy = cls.env["ai.copilot.allowed.model"].sudo().search([("model_name", "=", "res.partner")], limit=1)
        cls.policy.write({"enabled": True, "mcp_fields_csv": "id,name,color,company_id", "mcp_enabled": True})
        cls.query = cls.env["ai.copilot.query"].with_user(cls.reader).with_context(tradeline_mcp_reporting=True)

    def test_default_access_not_granted_to_administrator(self):
        admin = self.env.ref("base.user_admin")
        self.assertFalse(admin.has_group("tradeline_mcp_reporting.group_mcp_reporting_user"))
        with self.assertRaises(AccessError):
            self.bridge.with_user(admin).execute_tool("connection_info", {})

    def test_superuser_is_rejected(self):
        with self.assertRaises(AccessError):
            self.bridge.sudo().execute_tool("connection_info", {})

    def test_global_off_switch(self):
        self.settings.enable_mcp_reporting = False
        with self.assertRaises(AccessError):
            self.bridge.execute_tool("connection_info", {})

    def test_unknown_tools_context_and_exports_rejected(self):
        for tool, args in [("write", {}), ("query", {"spec": {}, "context": {"uid": 1}}),
                           ("query", {"spec": {}, "export": True}), ("query", {})]:
            with self.subTest(tool=tool, args=args):
                with self.assertRaises(ValidationError):
                    self.bridge.execute_tool(tool, args)

    def test_unapproved_models_denied_but_ordinary_chat_unaffected(self):
        self.policy.mcp_enabled = False
        with self.assertRaises(AccessError):
            self.query.describe_model("res.partner")
        ordinary = self.query.with_context(tradeline_mcp_reporting=False)
        self.assertIn("name", ordinary.describe_model("res.partner")["fields"])

    def test_field_policy_and_related_labels(self):
        fields = self.query.describe_model("res.partner")["fields"]
        self.assertIn("name", fields)
        self.assertNotIn("phone", fields)
        self.assertNotIn("company_id", fields)  # Related model is not approved.
        with self.assertRaises(AccessError):
            self.query.execute_query({"model": "res.partner", "fields": ["phone"]})

    def test_core_field_restrictions_still_apply(self):
        self.policy.allowed_fields_csv = "id,name"
        self.assertNotIn("color", self.query.describe_model("res.partner")["fields"])

    def test_related_label_core_policy_also_applies(self):
        related = self.env["ai.copilot.allowed.model"].sudo().search([("model_name", "=", "res.company")], limit=1)
        related.write({"enabled": True, "mcp_enabled": True, "mcp_fields_csv": "id,name", "allowed_fields_csv": False})
        self.assertIn("company_id", self.query.describe_model("res.partner")["fields"])
        related.allowed_fields_csv = "id"
        self.assertNotIn("company_id", self.query.describe_model("res.partner")["fields"])

    def test_related_record_rule_masks_sudo_display_names(self):
        hidden = self.env["res.partner"].create({"name": "MCP hidden parent"})
        child = self.env["res.partner"].create({"name": "MCP visible child", "parent_id": hidden.id})
        self.env["ir.rule"].create({"name": "MCP related name visibility", "model_id": self.env.ref("base.model_res_partner").id,
            "domain_force": "[('id','!=',%d)] if user.id == %d else [(1,'=',1)]" % (hidden.id, self.reader.id)})
        self.policy.mcp_fields_csv = "id,name,parent_id"
        result = self.query.execute_query({"model": "res.partner", "domain": [["id", "=", child.id]], "fields": ["id", "parent_id"]})
        self.assertEqual(result["rows"], [{"id": child.id, "parent_id": None}])
        self.assertEqual(result["provenance"]["redacted_related_cells"], {"parent_id": 1})

    def test_no_automatic_approval_from_catalogue_refresh(self):
        self.env["ai.copilot.service"].refresh_allowed_models()
        other = self.env["ai.copilot.allowed.model"].sudo().search([("model_name", "=", "res.company")], limit=1)
        self.assertFalse(other.mcp_enabled)
        discovered = self.query.search_models()
        self.assertEqual({row["model"] for row in discovered}, {"res.partner"})

    def test_explicit_fields_required(self):
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self.policy.write({"mcp_fields_csv": ""})

    def test_all_dispatch_targets_exist(self):
        from odoo.addons.tradeline_mcp_reporting.models.bridge import TOOLS
        for method, allowed, required in TOOLS.values():
            if method:
                self.assertTrue(callable(getattr(self.query, method)))

    def test_company_scope_uses_only_assigned_companies_and_keeps_main_first(self):
        user = SimpleNamespace(company_id=SimpleNamespace(id=7), company_ids=SimpleNamespace(ids=[9, 7, 3]))
        self.assertEqual(self.bridge.with_context(allowed_company_ids=[999])._reporting_company_ids(user), [7, 3, 9])

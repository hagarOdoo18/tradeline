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

    def test_business_scope_snapshot_preserves_restrictions_and_excludes_secrets(self):
        from odoo.addons.tradeline_mcp_reporting.models.scope import scope_manifest, FIELD_DENY, excluded_model
        manifest = {e["model"]: e for e in scope_manifest(self.env, self.reader)}
        self.assertIn("name", manifest["res.partner"]["fields"])
        for name in ["password", "access_token", "consumer_key", "api_key", "national_id", "iban"]:
            self.assertTrue(FIELD_DENY.search(name), name)
        self.assertTrue(excluded_model(self.env["ai.copilot.service"], "res.users"))
        self.policy.enabled = False
        manifest = {e["model"]: e for e in scope_manifest(self.env, self.reader)}
        self.assertEqual(manifest["res.partner"]["fields"], [])
        self.assertEqual(manifest["res.partner"]["reason"], "Existing Copilot model restriction")

    def test_coverage_is_paged_and_does_not_return_record_values(self):
        bridge = self.bridge.with_context(tradeline_mcp_reporting=True)
        result = bridge._reporting_coverage(keyword="partner")
        self.assertTrue(result["installed_module_count"])
        self.assertLessEqual(len(result["datasets"]), 60)
        entry = next(e for e in result["datasets"] if e["model"] == "res.partner")
        self.assertTrue(entry["available"])
        self.assertNotIn("fields", entry)
        self.policy.mcp_enabled = False
        result = bridge._reporting_coverage(keyword="partner")
        entry = next(e for e in result["datasets"] if e["model"] == "res.partner")
        self.assertFalse(entry["available"])
        self.assertIn("Not approved", entry["reason"])
        with self.assertRaises(ValidationError):
            bridge._reporting_coverage(offset=True)

    def test_source_citations_preserve_exact_query_and_only_explicit_record_ids(self):
        from urllib.parse import parse_qs, urlsplit
        import json
        args = {"spec": {"model": "res.partner", "domain": [["name", "ilike", "<script>"]], "fields": ["id", "name"]}}
        result = self.bridge._attach_citation({"source": "res.partner", "provenance": {},
            "columns": ["id", "name"], "rows": [{"id": 42, "name": "<script>"}],
            "title": "Partner sample"}, "query", args)
        # Empty provenance is still a report; use a real provenance key here.
        result["provenance"]["model"] = "res.partner"
        result = self.bridge._attach_citation(result, "query", args)
        params = parse_qs(urlsplit(result["citation"]["path"]).query)
        self.assertEqual(json.loads(params["arguments"][0]), args)
        self.assertEqual(result["citation"]["records"], [{"id": 42, "path": "/odoo/res.partner/42"}])
        result = self.bridge._attach_citation({"source": "res.partner", "provenance": {"model": "res.partner"},
            "columns": ["name", "__count"], "rows": [{"name": "Sample", "__count": 1}]}, "query", {})
        self.assertNotIn("records", result["citation"])

    def test_source_receipt_escapes_record_content_and_denies_unavailable_reports(self):
        from unittest.mock import MagicMock, patch
        from odoo.addons.tradeline_mcp_reporting.controllers import source
        fake = MagicMock()
        fake.env.__getitem__.return_value.execute_tool.return_value = {
            "title": "<script>alert(1)</script>", "columns": ["name"], "rows": [{"name": "<img src=x onerror=alert(1)>"}]}
        fake.make_response.side_effect = lambda body, **kwargs: source.http.Response(body, **kwargs)
        with patch.object(source, "request", fake):
            response = source.MCPSource().source(tool="query", arguments="{}")
            body = response.get_data(as_text=True)
            self.assertNotIn("<script>", body)
            self.assertNotIn("<img ", body)
            self.assertIn("&lt;script&gt;", body)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            fake.env.__getitem__.return_value.execute_tool.side_effect = AccessError("Private exception detail")
            response = source.MCPSource().source(tool="query", arguments="{}")
            body = response.get_data(as_text=True)
            self.assertEqual(response.status_code, 403)
            self.assertNotIn("Private exception detail", body)

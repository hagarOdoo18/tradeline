from types import SimpleNamespace
from unittest import TestCase as PythonTestCase
from unittest.mock import MagicMock, patch

from odoo.exceptions import AccessError, ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestMCPLiveShopify(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.settings = cls.env["ai.copilot.settings"].sudo().get_singleton()
        cls.settings.write({"enable_mcp_reporting": True, "enable_mcp_live_shopify": False})
        cls.reader = cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": "MCP Shopify QA reader", "login": "mcp_shopify_qa_reader",
            "groups_id": [(6, 0, cls.env.ref("tradeline_mcp_reporting.group_mcp_reporting_user").ids)]})
        cls.admin = cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": "MCP Shopify QA admin", "login": "mcp_shopify_qa_admin",
            "groups_id": [(6, 0, (cls.env.ref("tradeline_mcp_reporting.group_mcp_reporting_user") |
                                  cls.env.ref("base.group_system")).ids)]})
        cls.bridge = cls.env["tradeline.mcp.reporting"].with_user(cls.admin).with_context(tradeline_mcp_reporting=True)

    def test_separate_opt_in_admin_and_mcp_gates(self):
        self.assertFalse(self.bridge._live_shopify_enabled())
        with self.assertRaises(AccessError):
            self.bridge._live_shopify({"operation": "stores"})
        self.settings.enable_mcp_live_shopify = True
        self.assertTrue(self.bridge._live_shopify_enabled())
        with self.assertRaises(AccessError):
            self.bridge.with_user(self.reader)._live_shopify({"operation": "stores"})
        with self.assertRaises(AccessError):
            self.bridge.sudo()._live_shopify({"operation": "stores"})
        self.settings.enable_mcp_reporting = False
        with self.assertRaises(AccessError):
            self.bridge._live_shopify({"operation": "stores"})

    def test_fixed_surface_rejects_arbitrary_requests_before_network(self):
        with patch.object(type(self.bridge), "_live_shopify_engine") as engine:
            for spec in [{"url": "https://evil.invalid"}, {"query": "mutation{}"},
                         {"operation": "sync"}, {"operation": []}, {"instance_id": True},
                         {"instance_id": -1}, {"limit": 501}, {"limit": False}, {"title": 3}]:
                with self.subTest(spec=spec), self.assertRaises(ValidationError):
                    self.bridge._live_shopify(spec)
            engine.assert_not_called()

    def test_store_discovery_returns_only_fixed_metadata_without_live_fetch(self):
        engine = MagicMock()
        engine._shopify_instances.return_value = [SimpleNamespace(id=7, name="QA store", company_id=SimpleNamespace(id=3))]
        with patch.object(type(self.bridge), "_live_shopify_engine", return_value=engine):
            result = self.bridge._dispatch("live_shopify", {"spec": {"operation": "stores"}})
        self.assertEqual(result["rows"], [{"instance_id": 7, "store": "QA store", "company_id": 3}])
        engine.live_shopify.assert_not_called()
        self.assertIn("no API connection has been tested", result["provenance"]["scope"])
        cited = self.bridge._attach_citation(result, "live_shopify", {"spec": {"operation": "stores"}})
        self.assertIn("tool=live_shopify", cited["citation"]["path"])

    def test_live_read_reuses_fixed_backend_and_refuses_partial_output(self):
        engine = MagicMock()
        spec = {"operation": "orders_summary", "instance_id": 7, "date_from": "2026-10-20",
                "date_to": "2026-10-26", "financial_status": "paid", "fulfillment_status": "unfulfilled"}
        engine.live_shopify.return_value = {"rows": [{"orders": 3}], "truncated": False}
        with patch.object(type(self.bridge), "_live_shopify_engine", return_value=engine):
            self.assertEqual(self.bridge._live_shopify(spec)["rows"], [{"orders": 3}])
            engine.live_shopify.assert_called_once_with(spec)
            engine.live_shopify.return_value = {"rows": [{"orders": 3}], "truncated": True}
            with self.assertRaisesRegex(ValidationError, "truncated"):
                self.bridge._live_shopify(spec)

    def test_generic_queries_remain_restricted_when_live_tool_enabled(self):
        self.settings.enable_mcp_live_shopify = True
        engine = self.env["ai.copilot.query"].with_user(self.admin).with_context(tradeline_mcp_reporting=True)
        with PythonTestCase.assertRaises(self, (AccessError, ValidationError)):
            engine.describe_model("shopify.configuration")

    def test_narrow_internal_policy_context_retains_identity_and_company_scope(self):
        self.settings.enable_mcp_live_shopify = True
        env_type = type(self.env)
        original_contains = env_type.__contains__
        policies = self.env["ai.copilot.allowed.model"]
        sentinel = self.env["ai.copilot.allowed.model"].sudo().search([], limit=1)
        with patch.object(env_type, "__contains__", lambda env, name: name == "shopify.configuration" or original_contains(env, name)), \
             patch.object(type(policies), "search", return_value=sentinel):
            engine = self.bridge._live_shopify_engine()
        self.assertEqual(engine.env.uid, self.admin.id)
        self.assertFalse(engine.env.su)
        self.assertEqual(engine.env.companies, self.bridge.env.companies)
        self.assertFalse(engine.env.context["tradeline_mcp_reporting"])
        self.assertTrue(self.bridge.env.context["tradeline_mcp_reporting"])

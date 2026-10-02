from contextlib import ExitStack
from datetime import date
from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestProgressiveService(TransactionCase):
    def setUp(self):
        super().setUp()
        self.service = self.env["tradeline.executive.dashboard.service"]
        self.scope = {"start_date": date(2026, 1, 1), "end_date": date(2026, 1, 31),
                      "report_date": date(2026, 1, 31), "company_ids": [self.env.company.id],
                      "branch_ids": [], "salesperson_ids": []}

    def test_shell_does_not_run_accounting_queries(self):
        cls = type(self.service)
        with ExitStack() as stack:
            stack.enter_context(patch.object(cls, "_ensure_exec_admin"))
            stack.enter_context(patch.object(cls, "_resolve_filter_scope", return_value=self.scope))
            for method in ("_finance_summary", "_sales_summary", "_inventory_summary", "_build_top_sections", "get_drilldown"):
                stack.enter_context(patch.object(cls, method, side_effect=AssertionError("Expensive shell query")))
            shell = self.service.get_dashboard_shell()
        self.assertEqual(shell["cards"], [])
        self.assertEqual(shell["meta"]["scope"]["start_date"], "2026-01-01")
        self.assertTrue(shell["filter_options"]["companies"])

    def test_overview_preserves_financial_values_without_hidden_sections(self):
        cls = type(self.service)
        with ExitStack() as stack:
            for name, value in {
                "_ensure_exec_admin": None, "_resolve_filter_scope": self.scope,
                "_real_margin_availability": {"available": False},
                "_finance_summary": {"net_revenue": 1234, "collections_total": 456,
                                     "overdue_receivables": 100},
                "_sales_summary": {"invoice_count": 7},
                "_build_top_summary": {"today_sales": 99},
            }.items():
                stack.enter_context(patch.object(cls, name, return_value=value))
            for method in ("_inventory_summary", "_daily_sales_snapshot", "_build_top_sections",
                           "get_fx_watch", "_data_coverage", "get_drilldown"):
                stack.enter_context(patch.object(cls, method, side_effect=AssertionError("Hidden section queried")))
            data = self.service.get_dashboard_section("overview")
        values = {card["key"]: card["value"] for card in data["cards"]}
        self.assertEqual(values, {"net_revenue": 1234, "collections_total": 456,
                                  "overdue_receivables": 100, "invoice_count": 7})
        self.assertEqual(data["top_sections"]["today_sales"], 99)
        self.assertEqual(data["daily_top_sections"], {})
        self.assertEqual(data["drilldown"], {})

    def test_fx_section_uses_stored_history(self):
        cls = type(self.service)
        with patch.object(cls, "_ensure_exec_admin"), \
                patch.object(cls, "_resolve_filter_scope", return_value=self.scope), \
                patch.object(cls, "get_fx_watch", return_value={"cards": []}) as fx:
            self.service.get_dashboard_section("fx")
        fx.assert_called_once_with(allow_external=False)

    def test_progressive_endpoints_require_executive_access(self):
        with patch.object(type(self.service), "_ensure_exec_admin", side_effect=AccessError("Restricted")):
            with self.assertRaises(AccessError):
                self.service.get_dashboard_shell()
            with self.assertRaises(AccessError):
                self.service.get_dashboard_section("overview")

    def test_unknown_section_is_rejected(self):
        with patch.object(type(self.service), "_ensure_exec_admin"), \
                patch.object(type(self.service), "_resolve_filter_scope", return_value=self.scope):
            with self.assertRaises(ValueError):
                self.service.get_dashboard_section("unknown")

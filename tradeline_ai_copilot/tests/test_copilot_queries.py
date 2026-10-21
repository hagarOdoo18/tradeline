import base64
import io
import zipfile
import json
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import requests

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged
from odoo.addons.tradeline_ai_copilot.models.provider_stream import collect_provider_stream


class FakeProviderStream:
    def __init__(self, events):
        self.events = events
        self.closed = False

    def iter_lines(self, **kwargs):
        for event in self.events:
            yield "data: " + (event if isinstance(event, str) else json.dumps(event, ensure_ascii=False))
            yield ""

    def raise_for_status(self):
        pass

    def close(self):
        self.closed = True


@tagged("post_install", "-at_install")
class TestCopilotQueries(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        def company(name, tax_id):
            values = {"name": name, "vat": tax_id, "company_type": "company"}
            if "company_size" in cls.env["res.partner"]._fields:
                values.update(company_size="small", company_device=10)
            partner = cls.env["res.partner"].with_context(no_vat_validation=True).create(values)
            return cls.env["res.company"].create({"name": name, "partner_id": partner.id})
        cls.company = company("Copilot QA company", "123456789")
        cls.other_company = company("Copilot QA restricted", "987654321")
        cls.user = cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": "Copilot Analyst", "login": "copilot_analyst_qa",
            "company_id": cls.company.id, "company_ids": [(6, 0, cls.company.ids)],
            "groups_id": [(6, 0, cls.env.ref("tradeline_ai_copilot.group_ai_copilot_user").ids)],
        })
        cls.partners = cls.env["res.partner"]
        for values in [
            {"name": "Copilot Alpha", "company_id": cls.company.id, "color": 3},
            {"name": "Copilot Beta", "company_id": cls.company.id, "color": 7},
            {"name": "Copilot Restricted", "company_id": cls.other_company.id, "color": 99},
        ]:
            # Some installed contact customizations validate singleton records.
            cls.partners |= cls.env["res.partner"].create(values)
        cls.settings = cls.env["ai.copilot.settings"].sudo().get_singleton()
        cls.settings.write({"enable_flexible_queries": True, "enable_xlsx": True, "enable_csv": True,
                            "enable_history": True, "default_provider": "claude", "claude_api_key": "test-key-never-sent"})
        cls.service = cls.env["ai.copilot.service"].with_user(cls.user).with_context(allowed_company_ids=cls.company.ids)
        cls.query = cls.env["ai.copilot.query"].with_user(cls.user).with_context(allowed_company_ids=cls.company.ids)
        cls.policy = cls.env["ai.copilot.allowed.model"].sudo().search([("model_name", "=", "res.partner")], limit=1)
        cls.policy.write({"enabled": True, "allowed_fields_csv": False, "allow_export": True, "allow_groupby": True})

    def _spec(self, **kwargs):
        return dict({"model": "res.partner", "domain": [["id", "in", self.partners.ids]], "fields": ["name", "color"]}, **kwargs)

    def _financial_fixture(self):
        self.user.groups_id |= self.env.ref("account.group_account_manager")
        accounts = {}
        for code, category in [("99801", "income"), ("99802", "expense_direct_cost"), ("99803", "asset_current"), ("99804", "asset_receivable")]:
            accounts[category] = self.env["account.account"].create({"name": "QA " + category, "code": code,
                "account_type": category, "company_ids": [(6, 0, self.company.ids)], "reconcile": category == "asset_receivable"})
        journal = self.env["account.journal"].create({"name": "QA financial definitions", "code": "QAF", "type": "general", "company_id": self.company.id})
        moves = self.env["account.move"]
        for account, balance in [(accounts["income"], -1000), (accounts["income"], 200), (accounts["expense_direct_cost"], 600)]:
            move = self.env["account.move"].with_company(self.company).create({"date": "2026-10-01", "journal_id": journal.id,
                "line_ids": [(0, 0, {"name": "QA metric", "account_id": account.id, "debit": max(balance, 0), "credit": max(-balance, 0)}),
                             (0, 0, {"name": "QA counterpart", "account_id": accounts["asset_current"].id, "credit": max(balance, 0), "debit": max(-balance, 0)})]})
            move.action_post()
            moves |= move
        for name in ["account.move", "account.move.line", "account.account"]:
            self.service._check_model_access(name)[1].write({"enabled": True, "allow_groupby": True, "allow_export": True, "allowed_fields_csv": False})
        return accounts, moves

    def test_financial_ledger_signs_missing_cost_and_export(self):
        accounts, moves = self._financial_fixture()
        before = moves.read(["state", "write_date"])
        spec = {"metric": "ledger_gross_margin", "date_from": "2026-10-01", "date_to": "2026-10-01", "domain": [["move_id", "in", moves.ids]]}
        result = self.query.financial_report(spec)
        row = result["rows"][0]
        self.assertEqual((row["revenue"], row["cogs"], row["gross_profit"], row["margin_pct"]), (800, 600, 200, 25))
        self.assertIn("Not net profit", result["provenance"]["scope"])
        self.assertTrue(self.service.export_from_query_meta(result["query"], "xlsx")["file_id"])
        missing = self.query.financial_report(dict(spec, domain=[["move_id", "in", moves[:2].ids]]))["rows"][0]
        self.assertIsNone(missing["gross_profit"])
        self.assertIsNone(missing["margin_pct"])
        self.assertEqual(before, moves.read(["state", "write_date"]))

    def test_invoice_definition_uses_headers_discounts_refunds_and_invoice_date(self):
        accounts, unused = self._financial_fixture()
        journal = self.env["account.journal"].create({"name": "QA invoices", "code": "QAI", "type": "sale", "company_id": self.company.id})
        self.partners[0].with_company(self.company).property_account_receivable_id = accounts["asset_receivable"]
        invoices = self.env["account.move"]
        for move_type, amount, discount in [("out_invoice", 1000, 10), ("out_refund", 200, 0)]:
            invoice = self.env["account.move"].with_company(self.company).create({"move_type": move_type, "invoice_date": "2026-10-02", "date": "2026-10-03",
                "partner_id": self.partners[0].id, "journal_id": journal.id, "invoice_line_ids": [(0, 0,
                    {"name": "QA discounted sale", "account_id": accounts["income"].id, "quantity": 1, "price_unit": amount, "discount": discount, "tax_ids": [(5, 0, 0)]})]})
            invoice.action_post()
            invoices |= invoice
        result = self.query.financial_report({"metric": "invoice_net_revenue", "date_from": "2026-10-02", "date_to": "2026-10-02",
            "domain": [["id", "in", invoices.ids]], "grain": "day"})
        self.assertEqual(result["rows"][0]["net_revenue"], 700)
        self.assertEqual(result["rows"][0]["document_count"], 2)
        self.assertEqual(result["provenance"]["metric_definition"]["date_field"], "invoice_date")

    def test_financial_policy_and_source_freshness(self):
        before = self.partners.read(["write_date", "color"])
        result = self.query.data_freshness({"model": "res.partner", "domain": [["id", "in", self.partners.ids]], "date_field": "create_date"})
        self.assertEqual(result["rows"][0]["records"], 2)
        self.assertNotEqual(result["rows"][0]["earliest_business_date"], "Unknown")
        self.assertLessEqual(result["rows"][0]["earliest_business_date"], result["rows"][0]["latest_business_date"])
        self.assertIn("not proof", result["provenance"]["scope"])
        self.assertEqual(before, self.partners.read(["write_date", "color"]))
        self.policy.allowed_fields_csv = "name"
        with self.assertRaises(ValidationError):
            self.query.data_freshness({"model": "res.partner", "date_field": "create_date"})
        for dates in [("bad", "2026-10-01"), ("2026-10-02", "2026-10-01")]:
            with self.assertRaises(ValidationError):
                self.query._report_dates(dict(zip(("date_from", "date_to"), dates)))

    def _live_shopify_fixture(self):
        instance = SimpleNamespace(id=91, name="QA Store", shop_name="copilot-qa.myshopify.com", version="2026-10",
            company_id=self.company, access_token="fake-private-shopify-key", token_expiry=datetime.now() + timedelta(days=1), order_last_synced=False)
        class InstanceSet:
            def __len__(self): return 1
            def filtered(self, predicate): return self if predicate(instance) else []
            def __getattr__(self, key): return getattr(instance, key)
        connection = {"currentAppInstallation": {"accessScopes": [{"handle": "read_orders"}]},
                      "shop": {"name": "QA Store", "ianaTimezone": "Africa/Cairo", "currencyCode": "EGP"}}
        def order(key, total="114.00", tax="14.00", refund="0.00", **kwargs):
            return {"id": str(key), "name": "#" + str(key), "createdAt": "2026-10-20T22:30:00Z", "updatedAt": "2026-10-21T01:00:00Z",
                "cancelledAt": None, "test": False, "displayFinancialStatus": "PAID", "displayFulfillmentStatus": "UNFULFILLED",
                **{k: {"shopMoney": {"amount": a, "currencyCode": "EGP"}} for k, a in zip(
                    ["currentTotalPriceSet", "currentTotalTaxSet", "totalRefundedSet"], [total, tax, refund])}, **kwargs}
        return instance, InstanceSet(), connection, order

    def test_live_shopify_pagination_timezone_currency_status_and_no_writes(self):
        instance, instances, connection, order = self._live_shopify_fixture()
        pages = [(connection, "2026-10"), ({"orders": {"nodes": [order(1, refund="20")], "pageInfo": {"hasNextPage": True, "endCursor": "next"}}}, "2026-10"),
                 ({"orders": {"nodes": [order(2, test=True)], "pageInfo": {"hasNextPage": False, "endCursor": "end"}}}, "2026-10")]
        with patch.object(type(self.query), "_shopify_instances", return_value=instances), \
             patch.object(type(self.query), "_analysis_model", return_value=(self.env["res.partner"], self.policy)), \
             patch.object(type(self.query), "_shopify_transport", side_effect=pages) as transport:
            result = self.query.live_shopify({"operation": "orders_summary", "date_from": "2026-10-21", "date_to": "2026-10-21", "test_orders": "no"})
        row = result["rows"][0]
        self.assertEqual((row["orders"], row["current_order_value"], row["lifetime_refunded"]), (1, 114, 20))
        self.assertIn("+03:00", transport.call_args.args[2]["filter"])
        self.assertEqual(result["analysis"]["sources"][0]["complete_orders_read"], 2)
        self.assertEqual(result["provenance"]["source_kind"], "live_shopify")
        self.assertNotIn(instance.access_token, json.dumps(result))
        with self.assertRaises(AccessError):
            self.query._shopify_instances()

    def test_live_shopify_refuses_partial_old_scope_and_arbitrary_requests(self):
        instance, instances, connection, order = self._live_shopify_fixture()
        with patch.object(type(self.query), "_shopify_instances", return_value=instances), \
             patch.object(type(self.query), "_shopify_transport", return_value=(connection, "2026-10")), self.assertRaisesRegex(ValidationError, "read_all_orders"):
            self.query.live_shopify({"operation": "orders_summary", "date_from": "2020-01-01", "date_to": "2020-01-02"})
        partial = ({"orders": {"nodes": [order(1)], "pageInfo": {"hasNextPage": True, "endCursor": None}}}, "2026-10")
        with patch.object(type(self.query), "_shopify_instances", return_value=instances), \
             patch.object(type(self.query), "_shopify_transport", side_effect=[(connection, "2026-10"), partial]), self.assertRaisesRegex(ValidationError, "pagination"):
            self.query.live_shopify({"operation": "orders_summary", "date_from": "2026-10-21", "date_to": "2026-10-21"})
        from odoo.addons.tradeline_ai_copilot.models.ai_copilot_shopify import CONNECTION_QUERY
        for host in ["http://127.0.0.1", "valid.myshopify.com.evil.test", "user@valid.myshopify.com", "valid.myshopify.com/path"]:
            instance.shop_name = host
            with self.assertRaises(ValidationError):
                self.query._shopify_transport(instance, CONNECTION_QUERY, {}, time.monotonic() + 25)
        with self.assertRaises(AccessError):
            self.query._shopify_transport(instance, "mutation { deleteEverything }", {}, time.monotonic() + 25)

    def test_shopify_transport_redacts_errors_blocks_redirects_and_never_refreshes(self):
        instance, unused, connection, unused_order = self._live_shopify_fixture()
        from odoo.addons.tradeline_ai_copilot.models.ai_copilot_shopify import CONNECTION_QUERY
        with patch("odoo.addons.tradeline_ai_copilot.models.ai_copilot_shopify.requests.post") as post:
            post.return_value.status_code = 200
            post.return_value.headers = {"X-Shopify-API-Version": "2026-10"}
            post.return_value.iter_content.return_value = [json.dumps({"data": connection}).encode()]
            result, version = self.query._shopify_transport(instance, CONNECTION_QUERY, {}, time.monotonic() + 25)
            self.assertEqual(result, connection)
            self.assertFalse(post.call_args.kwargs["allow_redirects"])
            self.assertTrue(post.return_value.close.called)
            post.return_value.status_code = 302
            with self.assertRaises(ValidationError) as error:
                self.query._shopify_transport(instance, CONNECTION_QUERY, {}, time.monotonic() + 25)
            self.assertNotIn(instance.access_token, str(error.exception))
            post.reset_mock()
            instance.token_expiry = datetime.now() - timedelta(seconds=1)
            with self.assertRaisesRegex(ValidationError, "expired"):
                self.query._shopify_transport(instance, CONNECTION_QUERY, {}, time.monotonic() + 25)
            post.assert_not_called()

    def test_company_record_rules_and_current_user(self):
        result = self.query.execute_query(self._spec())
        self.assertEqual({row["name"] for row in result["rows"]}, {"Copilot Alpha", "Copilot Beta"})
        self.assertEqual(self.partners[2].color, 99)

    def test_aggregation_uses_all_matching_rows(self):
        result = self.query.execute_query(self._spec(group_by=["company_id"], aggregates=["color:sum"], limit=1))
        self.assertEqual(result["rows"][0]["color:sum"], 10)
        self.assertEqual(result["rows"][0]["__count"], 2)

    def test_count_without_grouping(self):
        result = self.query.execute_query(self._spec(aggregates=["__count"]))
        self.assertEqual(result["rows"], [{"__count": 2}])

    def test_limit_marks_incomplete_results(self):
        result = self.query.execute_query(self._spec(limit=1))
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["rows"]), 1)

    def test_rejects_technical_and_credential_models(self):
        for model in ["ir.config_parameter", "res.users", "ai.copilot.settings", "ai.copilot.message"]:
            with self.subTest(model=model), self.assertRaises(AccessError):
                self.query.execute_query({"model": model, "domain": [], "fields": ["name"]})

    def test_policy_applies_to_read_filter_sort_group_and_aggregate(self):
        self.policy.allowed_fields_csv = "name"
        specs = [self._spec(), self._spec(fields=["name"], domain=[["color", "=", 3]]),
                 self._spec(fields=["name"], order="color desc"),
                 self._spec(fields=[], group_by=["color"]), self._spec(fields=[], aggregates=["color:sum"])]
        for spec in specs:
            with self.subTest(spec=spec), self.assertRaises(UserError):
                self.query.execute_query(spec)

    def test_rejects_unsafe_filters_and_order(self):
        for spec in [self._spec(domain=[["name", "any", []]]), self._spec(domain=[["name", "=", {"code": "x"}]]),
                     self._spec(order="name desc; DROP TABLE res_partner"), self._spec(domain=["|", ["name", "=", "x"]]),
                     self._spec(domain=[["company_id.partner_id.user_ids.password", "=", "x"]]), self._spec(limit=-1)]:
            with self.subTest(spec=spec), self.assertRaises(UserError):
                self.query.execute_query(spec)

    def test_disabled_policy_stays_disabled_after_refresh(self):
        self.policy.enabled = False
        self.env["ai.copilot.service"].refresh_allowed_models()
        self.assertFalse(self.policy.enabled)
        with self.assertRaises(AccessError):
            self.query.execute_query(self._spec())

    def test_export_checks_current_policy_and_grouping(self):
        spec = self._spec(group_by=["company_id"], aggregates=["color:sum"])
        exported = self.service.export_from_query_meta(spec, "xlsx")
        generated = self.env["ai.copilot.generated.file"].browse(exported["file_id"])
        contents = zipfile.ZipFile(io.BytesIO(base64.b64decode(generated.attachment_id.datas)))
        self.assertIn(b"Color Index (sum)", contents.read("xl/sharedStrings.xml"))
        self.assertIn(b"Source queried at (UTC)", contents.read("xl/sharedStrings.xml"))
        self.assertIn(b"Filters (Odoo domain)", contents.read("xl/sharedStrings.xml"))
        self.assertIn(b"<v>10</v>", contents.read("xl/worksheets/sheet2.xml"))
        self.policy.allow_export = False
        with self.assertRaises(AccessError):
            self.service.export_from_query_meta(spec, "xlsx")

    def test_export_honors_disabled_format_and_sensitive_fields(self):
        self.settings.enable_xlsx = False
        with self.assertRaises(AccessError):
            self.service.export_from_query_meta(self._spec(), "xlsx")
        with self.assertRaises(AccessError):
            self.service.export_from_query_meta({"model_name": "res.partner", "fields": ["not_a_real_field"], "domain": []}, "csv")

    def test_spreadsheet_formula_text_is_not_executable(self):
        generated = self.service._export_xlsx([{"Name": "=1+1"}], ["Name"], False, "res.partner", "", "", "")
        archive = zipfile.ZipFile(io.BytesIO(base64.b64decode(generated.attachment_id.datas)))
        self.assertNotIn(b"<f>", archive.read("xl/worksheets/sheet2.xml"))
        csv = self.service._export_csv([{"Name": "=1+1"}], ["Name"], False, "res.partner")
        self.assertIn(b"'=1+1", base64.b64decode(csv.attachment_id.datas))

    def test_only_read_tools_are_dispatchable(self):
        for name in ["write", "create", "unlink", "execute", "sudo", "confirm_order"]:
            with self.subTest(name=name), self.assertRaises(AccessError):
                self.service._agent_dispatch(name, {}, [], False, "claude", "test")

    def test_claude_tool_loop_and_followup_history(self):
        conversation = self.env["ai.copilot.conversation"].with_user(self.user).create({"name": "Executive QA"})
        responses = [
            {"content": [{"type": "tool_use", "id": "q1", "name": "query_data", "input": self._spec(group_by=["company_id"], aggregates=["color:sum"])}]},
            {"content": [{"type": "text", "text": "The sum is 10 across two accessible records."}], "stop_reason": "end_turn"},
        ]
        with patch.object(type(self.service), "_agent_request", side_effect=responses) as mock:
            answer = self.env["ai.copilot.ui"].with_user(self.user).with_context(allowed_company_ids=self.company.ids).chat_send({
                "message": "Sum colors by company", "conversation_id": conversation.id, "provider": "claude"})
            self.assertEqual(mock.call_count, 2)
        self.assertTrue(any(block["type"] == "table" for block in answer["blocks"]))
        history = self.service._agent_history("What about last month?", conversation)
        self.assertIn("Sum colors", history[0]["content"])
        self.assertEqual(history[-1]["content"], "What about last month?")
        self.assertEqual(self.partners.mapped("color"), [3, 7, 99])

    def test_openai_tool_protocol(self):
        responses = [
            {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{"id": "o1", "type": "function", "function": {"name": "search_models", "arguments": '{"keyword":"partner"}'}}]}}]},
            {"choices": [{"message": {"role": "assistant", "content": "Customer data is available."}, "finish_reason": "stop"}]},
        ]
        self.settings.openai_api_key = "fake-not-sent"
        with patch.object(type(self.service), "_agent_request", side_effect=responses):
            result = self.service.generate_response("Find customer data", provider="openai")
        self.assertEqual(result["blocks"][0]["content"], "Customer data is available.")

    def test_provider_failure_is_explicit(self):
        with patch.object(type(self.service), "_agent_request", side_effect=requests.ConnectionError("offline")):
            result = self.service.generate_response("Sales this month", provider="claude")
        self.assertTrue(any(block["type"] == "warning" for block in result["blocks"]))
        self.assertFalse(any(block["type"] == "table" for block in result["blocks"]))

    def test_cross_user_chat_cannot_load_or_export(self):
        other = self.env["ai.copilot.conversation"].create({"name": "Private admin conversation"})
        with self.assertRaises(AccessError):
            self.env["ai.copilot.ui"].with_user(self.user).load_conversation(other.id)
        with self.assertRaises(AccessError):
            self.service.export_from_query_meta(self._spec(), "csv", other.id)

    def test_chart_uses_verified_groups_and_handles_negative_values(self):
        result = {"query": {"group_by": ["name"], "aggregates": ["color:sum"]}, "labels": {"color:sum": "Color"},
                  "rows": [{"name": "A", "color:sum": 3}, {"name": "B", "color:sum": -1}], "truncated": False}
        chart = self.service._agent_chart(result, "doughnut", "Comparison")
        self.assertEqual(chart["chart_type"], "bar")
        self.assertEqual(chart["data"]["datasets"][0]["data"], [3, -1])
        result["query"] = {}
        self.assertFalse(self.service._agent_chart(result, "bar", "Raw preview"))

    def test_query_timeout_is_restored(self):
        self.env.cr.execute("SELECT current_setting('statement_timeout')")
        before = self.env.cr.fetchone()[0]
        self.query.execute_query(self._spec())
        self.env.cr.execute("SELECT current_setting('statement_timeout')")
        self.assertEqual(self.env.cr.fetchone()[0], before)

    def test_model_discovery_and_schema_obey_policy(self):
        discovered = self.query.search_models("partner")
        self.assertIn("res.partner", [item["model"] for item in discovered])
        self.policy.allowed_fields_csv = "name,city"
        schema = self.query.describe_model("res.partner")
        self.assertEqual(set(schema["fields"]), {"name", "city"})
        self.policy.enabled = False
        self.assertNotIn("res.partner", [item["model"] for item in self.query.search_models("partner")])

    def test_provider_http_payloads_use_native_protocols(self):
        with patch("odoo.addons.tradeline_ai_copilot.models.ai_copilot_agent.requests.post") as post:
            post.return_value.json.return_value = {"content": []}
            self.service._agent_request("claude", "test-model", [{"role": "user", "content": "Hello"}], self.service._agent_tools(), 10)
            self.assertEqual(post.call_args.args[0], "https://api.anthropic.com/v1/messages")
            self.assertIn("input_schema", post.call_args.kwargs["json"]["tools"][0])
            self.settings.openai_api_key = "fake-not-sent"
            self.service._agent_request("openai", "test-model", [{"role": "user", "content": "Hello"}], self.service._agent_tools(), 10)
            self.assertEqual(post.call_args.kwargs["json"]["tools"][0]["type"], "function")
        bootstrap = self.env["ai.copilot.ui"].with_user(self.user).bootstrap()
        self.assertNotIn("api_key", str(bootstrap))

    def test_long_text_and_empty_shares_are_explicit(self):
        self.partners[0].name = "A" * 3000
        result = self.query.execute_query(self._spec(fields=["name"]))
        self.assertTrue(result["cells_truncated"])
        self.assertEqual(len(result["rows"][0]["name"]), 2001)
        chart_result = {"query": {"group_by": ["name"], "aggregates": ["__count"]},
                        "labels": {"__count": "Records"}, "truncated": False,
                        "rows": [{"name": "A", "__count": 0}, {"name": "B", "__count": 0}]}
        self.assertEqual(self.service._agent_chart(chart_result, "doughnut", "Zero shares")["chart_type"], "bar")

    def _consume(self, iterator):
        events = []
        while True:
            try:
                events.append(next(iterator))
            except StopIteration as done:
                return events, done.value

    def _claude_text(self, parts):
        return [
            {"type": "message_start", "message": {"content": []}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            *[{"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": part}} for part in parts],
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}, {"type": "message_stop"},
        ]

    def test_claude_stream_is_incremental_and_preserves_arabic(self):
        response = FakeProviderStream(self._claude_text(["المبيعات ", "حسب الفرع.", "\n\nRead-only report."]))
        events, final = self._consume(collect_provider_stream(response, "claude", time.monotonic() + 30))
        self.assertEqual([event["text"] for event in events], ["المبيعات ", "حسب الفرع.", "\n\nRead-only report."])
        self.assertEqual(final["content"][0]["text"], "المبيعات حسب الفرع.\n\nRead-only report.")

    def test_stream_reassembles_tool_arguments_before_querying(self):
        response = FakeProviderStream([
            {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "q1", "name": "query_data", "input": {}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"model":"res.'}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": 'partner","domain":[]}' }},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}}, {"type": "message_stop"},
        ])
        events, final = self._consume(collect_provider_stream(response, "claude", time.monotonic() + 30))
        self.assertEqual(events, [])
        self.assertEqual(final["content"][0]["input"], {"model": "res.partner", "domain": []})

    def test_openai_stream_reassembles_content_and_tools(self):
        response = FakeProviderStream([
            {"choices": [{"delta": {"content": "A report. ", "tool_calls": [{"index": 0, "id": "c1", "function": {"name": "search_models", "arguments": '{"keyword":'}}]}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "More detail.", "tool_calls": [{"index": 0, "function": {"arguments": '"sale"}'}}]}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}, "[DONE]",
        ])
        events, final = self._consume(collect_provider_stream(response, "openai", time.monotonic() + 30))
        message = final["choices"][0]["message"]
        self.assertEqual(len(events), 2)
        self.assertEqual(message["content"], "A report. More detail.")
        self.assertEqual(json.loads(message["tool_calls"][0]["function"]["arguments"]), {"keyword": "sale"})

    def test_sol_responses_round_trip_preserves_reasoning_and_read_only_results(self):
        self.settings.openai_api_key = "fake-not-sent"
        native_output = [
            {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque-test"},
            {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "query_data",
             "arguments": json.dumps(self._spec(aggregates=["__count"])), "status": "completed"},
        ]
        report = {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                  "content": [{"type": "output_text", "text": "There are 2 records [S1].", "annotations": []}]}
        streams = [FakeProviderStream([{"type": "response.completed", "response": {
            "status": "completed", "output": native_output}}]), FakeProviderStream([
                {"type": "response.output_text.delta", "delta": "There are "},
                {"type": "response.output_text.delta", "delta": "2 records [S1]."},
                {"type": "response.completed", "response": {"status": "completed", "output": [report]}}])]
        with patch("odoo.addons.tradeline_ai_copilot.models.ai_copilot_agent.requests.post", side_effect=streams) as post:
            events = list(self.service._generate_response_stream("Count the QA records", provider="openai", llm_model="gpt-6.1-sol"))
        self.assertEqual(post.call_count, 2)
        request = post.call_args.kwargs["json"]
        self.assertEqual(post.call_args.args[0], "https://api.openai.com/v1/responses")
        self.assertEqual(request["input"][1:3], native_output)
        self.assertEqual(request["input"][3]["type"], "function_call_output")
        self.assertEqual(request["input"][3]["call_id"], "call_1")
        self.assertFalse(request["store"])
        self.assertEqual(request["reasoning"], {"effort": "low"})
        self.assertNotIn("temperature", request)
        self.assertFalse(request["tools"][0]["strict"])
        self.assertEqual(len([e for e in events if e["type"] == "delta"]), 2)
        answer = events[-1]["response"]
        self.assertEqual(answer["blocks"][0]["content"], "There are 2 records [S1].")
        self.assertTrue(any(b["type"] == "table" and b["rows"] == [{"__count": 2}] for b in answer["blocks"]))
        self.assertTrue(all(s.closed for s in streams))
        self.assertEqual(self.partners.mapped("color"), [3, 7, 99])

    def test_luna_and_sol_use_responses_but_existing_chat_models_stay_compatible(self):
        self.settings.openai_api_key = "fake-not-sent"
        for model in ["gpt-6.1-sol", "gpt-6-luna"]:
            url, request, unused_headers = self.service._agent_request_parameters("openai", model,
                [{"role": "user", "content": "Count sales"}], self.service._agent_tools())
            self.assertTrue(url.endswith("/responses"))
            self.assertEqual(request["model"], model)
            self.assertIn("reasoning.encrypted_content", request["include"])
        url, request, unused_headers = self.service._agent_request_parameters("openai", "gpt-4.1-mini",
            [{"role": "user", "content": "Count sales"}], self.service._agent_tools())
        self.assertTrue(url.endswith("/chat/completions"))
        self.assertIn("function", request["tools"][0])

    def test_responses_truncated_tools_and_failed_streams_cannot_execute(self):
        for status in ["incomplete", "failed"]:
            response = FakeProviderStream([{"type": "response." + status, "response": {
                "status": status, "incomplete_details": {"reason": "max_output_tokens"}, "output": [{
                    "type": "function_call", "call_id": "bad", "name": "query_data",
                    "arguments": "{}", "status": "in_progress"}]}}])
            with self.subTest(status=status), self.assertRaises(ValueError):
                self._consume(collect_provider_stream(response, "openai_responses", time.monotonic() + 30))
        with self.assertRaises(ValueError):
            self._consume(collect_provider_stream(FakeProviderStream([
                {"type": "response.output_text.delta", "delta": "Partial"}]), "openai_responses", time.monotonic() + 30))

    def test_incomplete_stream_is_not_accepted_as_finished(self):
        response = FakeProviderStream(self._claude_text(["Partial answer"])[0:-2])
        with self.assertRaises(ValueError):
            self._consume(collect_provider_stream(response, "claude", time.monotonic() + 30))

    def test_streaming_chat_persists_final_report_after_deltas(self):
        response = FakeProviderStream(self._claude_text(["Here is ", "your read-only report."]))
        ui = self.env["ai.copilot.ui"].with_user(self.user).with_context(allowed_company_ids=self.company.ids)
        with patch("odoo.addons.tradeline_ai_copilot.models.ai_copilot_agent.requests.post", return_value=response) as post:
            events = list(ui._chat_stream({"message": "Explain reporting", "provider": "claude"}))
        self.assertTrue(post.call_args.kwargs["stream"])
        self.assertTrue(post.call_args.kwargs["json"]["stream"])
        self.assertTrue(response.closed)
        self.assertEqual(len([event for event in events if event["type"] == "delta"]), 2)
        self.assertEqual(events[-1]["type"], "complete")
        saved = self.env["ai.copilot.message"].browse(events[-1]["response"]["message_id"])
        self.assertEqual(saved.content, "Here is your read-only report.")
        self.assertEqual(self.partners.mapped("color"), [3, 7, 99])

    def test_stream_failure_replaces_partial_claim_with_warning(self):
        response = FakeProviderStream(self._claude_text(["Unfinished numbers"])[0:-2])
        with patch("odoo.addons.tradeline_ai_copilot.models.ai_copilot_agent.requests.post", return_value=response):
            events = list(self.service._generate_response_stream("Sales", provider="claude"))
        final = events[-1]["response"]
        self.assertTrue(any(block["type"] == "warning" for block in final["blocks"]))
        self.assertNotIn("Unfinished numbers", final["blocks"][0]["content"])
        self.assertTrue(response.closed)

    def test_stopping_chat_closes_provider_without_saving_partial_answer(self):
        response = FakeProviderStream(self._claude_text(["Partial ", "report."]))
        ui = self.env["ai.copilot.ui"].with_user(self.user).with_context(allowed_company_ids=self.company.ids)
        with patch("odoo.addons.tradeline_ai_copilot.models.ai_copilot_agent.requests.post", return_value=response):
            events = ui._chat_stream({"message": "Stop this test report", "provider": "claude"})
            conversation_id = next(events)["conversation_id"]
            while next(events)["type"] != "delta":
                pass
            events.close()
        self.assertTrue(response.closed)
        self.assertFalse(self.env["ai.copilot.message"].search_count([
            ("conversation_id", "=", conversation_id), ("role", "=", "assistant")]))

    def test_reporting_prompt_and_tools_cannot_operate_forms(self):
        prompt = self.service._agent_system_prompt()
        self.assertIn("cannot see, save or modify unsaved forms", prompt)
        self.assertEqual({tool["name"] for tool in self.service._agent_tools()}, {
            "request_clarification", "search_models", "describe_model", "query_data", "business_context", "compare_customer_sets", "analyze_baskets",
            "metric_definitions", "financial_report", "data_freshness"})

    def test_source_metadata_describes_executed_query_not_model_prose(self):
        result = self.query.execute_query(self._spec(group_by=["company_id"], aggregates=["color:sum"], limit=1))
        source = result["provenance"]
        self.assertEqual(source["model"], "res.partner")
        self.assertEqual(source["domain"], self._spec()["domain"])
        self.assertEqual(source["companies"], [{"id": self.company.id, "name": self.company.name}])
        self.assertEqual(source["aggregates"], ["color:sum"])
        self.assertEqual(source["group_by"], ["company_id"])
        self.assertEqual(source["returned_rows"], 1)
        self.assertTrue(source["queried_at"])

    def test_sources_and_citations_share_real_tool_results(self):
        results = []
        tool = self.service._agent_dispatch("query_data", self._spec(), results, False, "claude", "test")
        self.assertEqual(tool["source_id"], "S1")
        answer = self.service._finish_agent_response("Explain", False, "claude", "test", "The saved records are shown [S1].", results, [], True)
        table = next(block for block in answer["blocks"] if block["type"] == "table")
        self.assertEqual(table["provenance"]["source_id"], "S1")
        self.assertEqual(table["rows"], tool["rows"])
        self.assertFalse(any(block["type"] == "warning" for block in answer["blocks"]))
        unsupported = self.service._finish_agent_response("Explain", False, "claude", "test", "Trust me [S99].", results, [], True)
        self.assertTrue(any("unavailable source" in block.get("content", "") for block in unsupported["blocks"]))

    def test_no_query_answer_cannot_be_presented_as_data_evidence(self):
        answer = self.service._finish_agent_response("Explain", False, "claude", "test", "An explanation.", [], [], True)
        self.assertTrue(any("No Odoo data query" in block.get("content", "") for block in answer["blocks"]))

    def test_model_discovery_pagination_and_module_names(self):
        first = self.query.search_models("")
        following = self.query.search_models("", offset=1)
        self.assertEqual(first[1:], following[:len(first)-1])
        self.assertIn("res.partner", [m["model"] for m in self.query.search_models("base")])
        for offset in [-1, True, 10001, "60"]:
            with self.assertRaises(ValidationError):
                self.query.search_models("", offset=offset)

    def test_abstract_virtual_and_user_credential_helpers_are_denied(self):
        for name in ["publisher_warranty.contract", "res.users.apikeys.show", "account.root", "account.code.mapping"]:
            if name in self.env:
                with self.subTest(model=name), self.assertRaises(AccessError):
                    self.service._check_model_access(name)

    def test_broken_custom_selection_does_not_break_other_schema_fields(self):
        # Reproduce a missing callback without changing any custom module.
        selection = self.env["res.partner"]._fields["type"]
        with patch.object(selection, "selection", "_copilot_missing_callback"):
            schema = self.query.describe_model("res.partner")
            self.assertIn("name", schema["fields"])
            self.assertNotIn("type", schema["fields"])
            result = self.query.execute_query(self._spec(aggregates=["__count"]))
            self.assertEqual(result["rows"], [{"__count": 2}])

    def test_english_reporting_prompt_requires_clarification_and_sources(self):
        prompt = self.service._agent_system_prompt()
        self.assertIn("ordinary English, spelling mistakes", prompt)
        self.assertIn("Clarify ambiguous revenue or profit", prompt)
        self.assertIn("[S1]", prompt)
        self.assertIn("never earlier assistant prose", prompt)

    def _customer_cohort_fixture(self):
        children = self.env["res.partner"]
        for parent in [self.partners[0], self.partners[0], self.partners[1], False]:
            children |= self.env["res.partner"].create({"name": "Cohort saved purchase", "company_id": self.company.id,
                "parent_id": parent.id if parent else False})
        include = {"model": "res.partner", "domain": [["id", "in", children.ids]], "key_field": "parent_id"}
        exclude = dict(include, domain=[["id", "=", children[2].id]])
        return {"include": [include, include], "exclude": [exclude], "title": "QA cross-source cohort"}

    def test_cohort_deduplicates_union_and_difference_with_missing_identities(self):
        spec = self._customer_cohort_fixture()
        result = self.query.compare_customer_sets(spec)
        self.assertEqual(result["analysis"]["included_customers"], 2)
        self.assertEqual(result["analysis"]["eligible_customers"], 1)
        self.assertEqual(result["analysis"]["also_in_excluded_set"], 1)
        chart = self.service._agent_chart(result, "bar", "Matched customer populations")
        self.assertEqual(chart["data"]["datasets"][0]["data"], [1, 1])
        self.assertEqual(len(chart["data"]["labels"]), 2)
        preview = self.query.compare_customer_sets(dict(spec, show_customers=True, include_names=True))
        self.assertEqual(preview["rows"], [{"customer_id": self.partners[0].id, "customer_name": self.partners[0].name}])
        self.assertFalse(result["analysis"]["identity_coverage_complete"])
        self.assertTrue(result["analysis"]["absence_established_within_matched_scope"])

    def test_cohort_refuses_incomplete_sets_and_noncustomer_keys(self):
        spec = self._customer_cohort_fixture()
        with patch.object(type(self.query), "ANALYSIS_GROUP_LIMIT", 1), self.assertRaises(ValidationError):
            self.query.compare_customer_sets(spec)
        spec["include"][0] = {"model": "res.partner", "domain": [], "key_field": "company_id"}
        with self.assertRaises(ValidationError):
            self.query.compare_customer_sets(spec)

    def test_pos_cohort_resolves_header_customer_and_anonymous_rows(self):
        # Use real policy/relationship validation when POS is installed. The
        # controlled read population includes repeat and anonymous purchases.
        if "pos.order.line" not in self.env:
            self.skipTest("POS source is optional; production read-only verification covers it")
        self.user.groups_id |= self.env.ref("point_of_sale.group_pos_user")
        for name in ["pos.order.line", "pos.order"]:
            self.service._check_model_access(name)[1].write({"enabled": True, "allow_groupby": True, "allow_export": True})
        groups = [{"order_id": (1, "one"), "__count": 2}, {"order_id": (2, "two"), "__count": 1},
                  {"order_id": (3, "anonymous"), "__count": 4}]
        headers = [{"id": 1, "partner_id": (self.partners[0].id, "Alpha")},
                   {"id": 2, "partner_id": (self.partners[0].id, "Alpha")}, {"id": 3, "partner_id": False}]
        spec = {"model": "pos.order.line", "domain": [], "key_field": "order_id.partner_id"}
        with patch.object(type(self.query), "_analysis_groups", return_value=groups), \
                patch.object(type(self.env["pos.order"]), "search_read", return_value=headers):
            keys, detail = self.query._customer_set(spec, False, time.monotonic() + 25)
        self.assertEqual(keys, {self.partners[0].id})
        self.assertEqual((detail["matching_rows"], detail["unresolved_rows"]), (7, 4))
        for key in ["order_id.user_id", "order_id.partner_id.vat", "partner_id"]:
            with self.subTest(key=key), self.assertRaises(ValidationError):
                self.query._customer_set(dict(spec, key_field=key), False, time.monotonic() + 25)

    def test_legacy_order_identity_does_not_use_old_numeric_ids(self):
        if "legacy.sale.order.line" not in self.env:
            self.skipTest("Legacy sales order addon is optional")
        self.user.groups_id |= self.env.ref("sales_team.group_sale_salesman_all_leads")
        for name in ["legacy.sale.order", "legacy.sale.order.line"]:
            self.service._check_model_access(name)[1].write({"enabled": True, "allow_groupby": True, "allow_export": True})
        self.partners[0].mobile = "01012345678"
        order = self.env["legacy.sale.order"].create({"source_db": "QA", "source_order_id": self.partners[1].id,
            "name": "QA legacy order", "company_id": self.company.id, "date_order": "2025-12-01 12:00:00",
            "state": "sale", "source_partner_mobile": "+20 101 234 5678"})
        line = self.env["legacy.sale.order.line"].create({"order_id": order.id, "source_line_id": 123,
            "qty_ordered": 1, "qty_delivered": 1, "product_name": "QA handset"})
        before = (order.write_date, line.write_date, self.partners.mapped("write_date"))
        source = {"model": "legacy.sale.order.line", "key_field": "order_id", "domain": [["id", "=", line.id]],
                  "resolve_legacy_identifiers": True}
        keys, detail = self.query._customer_set(source, False, time.monotonic() + 25, {})
        self.assertEqual(keys, {self.partners[0].id})
        self.assertEqual(detail["identifier_resolution_rows"], {"phone": 1})
        self.assertNotIn("01012345678", json.dumps(detail))
        self.assertEqual(before, (order.write_date, line.write_date, self.partners.mapped("write_date")))
        with self.assertRaises(ValidationError):
            self.query._customer_set(dict(source, resolve_legacy_identifiers=False), False, time.monotonic() + 25)

    def test_unfinished_discovery_gets_one_bounded_completion_retry(self):
        responses = [
            {"content": [{"type": "text", "text": "I haven't completed the customer comparison. The next step is to finish matching."}]},
            {"content": [{"type": "tool_use", "id": "calculate", "name": "compare_customer_sets", "input": self._customer_cohort_fixture()}]},
            {"content": [{"type": "text", "text": "One matched customer has no purchase in the exclusion scope [S1]."}]},
        ]
        with patch.object(type(self.service), "_agent_request", side_effect=responses) as request:
            answer = self.service.generate_response("Compare buyers across sources", provider="claude")
        self.assertEqual(request.call_count, 3)
        self.assertTrue(any(b.get("analysis", {}).get("eligible_customers") == 1 for b in answer["blocks"] if b["type"] == "table"))
        self.assertNotIn("haven't completed", answer["blocks"][0]["content"])
        with patch.object(type(self.service), "_agent_request", return_value=responses[0]) as request:
            self.service.generate_response("Compare buyers across sources", provider="claude")
        self.assertEqual(request.call_count, 2)

    def test_analysis_timeout_gives_actionable_scope_error(self):
        from psycopg2.errors import QueryCanceled
        spec = self._customer_cohort_fixture()
        with patch.object(type(self.env["res.partner"]), "read_group", side_effect=QueryCanceled), self.assertRaisesRegex(ValidationError, "eight-second"):
            self.query.compare_customer_sets(spec)
        self.assertEqual(self.partners.mapped("color"), [3, 7, 99])

    def test_cohort_export_rechecks_source_policy_and_requeries(self):
        spec = self._customer_cohort_fixture()
        result = self.query.compare_customer_sets(spec)
        export = self.service.export_from_query_meta(result["query"], "xlsx")
        self.assertTrue(export["file_id"])
        self.policy.allow_export = False
        with self.assertRaises(AccessError):
            self.service.export_from_query_meta(result["query"], "xlsx")

    def _legacy_analysis_fixture(self):
        if "legacy.invoice.line" not in self.env:
            self.skipTest("Optional legacy archive is not installed in this test database")
        self.user.write({"groups_id": [(4, self.env.ref("account.group_account_readonly").id)]})
        invoices = self.env["legacy.invoice"]
        for i in range(3):
            invoice = self.env["legacy.invoice"].create({"source_db": "copilot-strategy-qa", "source_id": 800+i,
                "company_id": self.company.id, "currency_id": self.company.currency_id.id,
                "state": "paid", "invoice_type": "out_invoice", "invoice_date": "2026-01-01"})
            self.env["legacy.invoice.line"].create({"invoice_id": invoice.id, "source_id": 900+i, "name": "QA handset", "quantity": 1})
            invoices |= invoice
        return invoices

    def test_legacy_bridge_uses_only_unambiguous_accepted_links(self):
        invoices = self._legacy_analysis_fixture()
        bridge = self.env["legacy.invoice.customer.bridge"]
        for invoice, partner, status, method in [
            (invoices[0], self.partners[0], "linked", "manual"),
            (invoices[1], self.partners[0], "strict", "phone"),
            (invoices[1], self.partners[1], "strict", "email"),
            (invoices[2], self.partners[1], "ambiguous", "phone")]:
            bridge.create({"invoice_id": invoice.id, "source_partner_id": invoice.id,
                "target_partner_id": partner.id, "match_status": status, "match_method": method})
        source = {"model": "legacy.invoice.line", "domain": [["invoice_id", "in", invoices.ids]],
                  "key_field": "partner_id", "resolve_legacy_links": True}
        result = self.query.compare_customer_sets({"include": [source], "exclude": [source]})
        self.assertEqual(result["analysis"]["included_customers"], 1)
        self.assertEqual(result["analysis"]["eligible_customers"], 0)
        coverage = result["analysis"]["sources"][0]
        self.assertEqual(coverage["bridge_linked_rows"], 1)
        self.assertEqual(coverage["unresolved_rows"], 2)
        self.assertEqual(coverage["ambiguous_link_rows"], 1)
        self.assertFalse(result["analysis"]["absence_established_within_matched_scope"])

    def test_analysis_enforces_header_record_rules(self):
        invoices = self._legacy_analysis_fixture()
        invoices.partner_id = self.partners[0]
        self.env["ir.rule"].create({"name": "QA block one legacy header", "model_id": self.env["ir.model"]._get_id("legacy.invoice"),
            "domain_force": "[('id', '!=', %s)]" % invoices[0].id})
        result = self.query.compare_customer_sets({"include": [{"model": "legacy.invoice.line", "key_field": "partner_id",
            "domain": [["invoice_id", "in", invoices.ids]]}], "exclude": []})
        self.assertEqual(result["analysis"]["sources"][0]["matching_rows"], 2)

    def test_identity_normalization_and_conflict_rules(self):
        from odoo.addons.tradeline_ai_copilot.models.ai_copilot_identity import phone_key, national_key, identity_match
        for value in ["01012345678", "+20 101 234 5678", "0020-101-234-5678", "٠١٠١٢٣٤٥٦٧٨"]:
            self.assertEqual(phone_key(value), "201012345678")
        for value in ["555-1234", "01012345678 ext 5", "00000000000"]:
            self.assertFalse(phone_key(value))
        self.assertEqual(national_key("٢٩٠٠١٠١١٢٣٤٥٦٧"), "29001011234567")
        for value in ["123456789", "29013011234567", "39001011234567"]:
            self.assertFalse(national_key(value))
        index = {"national": {"id-a": {1}}, "phone": {"a": {1}, "shared": {1, 2}, "b": {2}}}
        self.assertEqual(identity_match(["id-a"], ["a"], index), (1, "national_id"))
        self.assertEqual(identity_match([], ["shared"], index), (False, "ambiguous"))
        self.assertEqual(identity_match(["id-a"], ["b"], index), (False, "conflicting"))
        self.assertFalse(identity_match(["unknown"], ["a"], index)[0])

    def test_legacy_identifiers_are_read_only_unique_and_not_sent_to_model(self):
        invoices = self._legacy_analysis_fixture()
        self.partners[0].write({"mobile": "01012345678", "vat": "29001011234567"})
        self.partners[1].write({"mobile": "01112345678", "vat": "29102021234567"})
        duplicate = self.env["res.partner"].create({"name": "QA archived duplicate", "mobile": "01112345678", "active": False})
        invoices[0].write({"source_partner_mobile": "+20 101 234 5678", "source_partner_national_id": "29001011234567"})
        invoices[1].write({"source_partner_mobile": "01112345678"})
        invoices[2].write({"source_partner_mobile": "01012345678", "source_partner_national_id": "29102021234567"})
        watched = invoices | self.env["legacy.invoice"]
        before = [(record.id, record.write_date, record.partner_id.id) for record in watched]
        partner_dates = (self.partners | duplicate).mapped("write_date")
        source = {"model": "legacy.invoice.line", "key_field": "partner_id", "domain": [["invoice_id", "in", invoices.ids]],
                  "resolve_legacy_links": True, "resolve_legacy_identifiers": True}
        result = self.query.compare_customer_sets({"include": [source], "exclude": [source]})
        coverage = result["analysis"]["sources"][0]
        self.assertEqual(result["analysis"]["included_customers"], 1)
        self.assertEqual(coverage["identifier_matched_rows"], 1)
        self.assertEqual(coverage["identifier_resolution_rows"], {"national_id": 1, "ambiguous": 1, "conflicting": 1})
        self.assertEqual(coverage["unresolved_rows"], 2)
        for value in ["29001011234567", "29102021234567", "01012345678", "201012345678"]:
            self.assertNotIn(value, json.dumps(result))
        self.assertEqual(before, [(record.id, record.write_date, record.partner_id.id) for record in watched])
        self.assertEqual(partner_dates, (self.partners | duplicate).mapped("write_date"))
        self.assertTrue(self.service.export_from_query_meta(result["query"], "xlsx")["file_id"])

    def test_identity_lookup_rejects_partial_index_and_field_policy_bypass(self):
        with patch.object(type(self.query), "IDENTITY_CONTACT_LIMIT", 1), self.assertRaisesRegex(ValidationError, "partial"):
            self.query._identity_index(False, time.monotonic() + 25)
        self.policy.allowed_fields_csv = "name"
        with self.assertRaises(AccessError):
            self.query._identity_index(False, time.monotonic() + 25)

    def test_legacy_line_phone_fallback_and_current_contact_record_rules(self):
        invoices = self._legacy_analysis_fixture()
        line = self.env["legacy.invoice.line"].search([("invoice_id", "=", invoices[0].id)])
        if "customer_phone" not in line._fields:
            self.skipTest("Optional line customer fields are not installed")
        self.partners[0].mobile = "01012345678"
        self.partners[2].mobile = "01012345678"  # other company, not visible to analyst
        line.customer_phone = "0020 1012345678"
        result = self.query.compare_customer_sets({"include": [{"model": "legacy.invoice.line", "key_field": "partner_id",
            "domain": [["invoice_id", "=", invoices[0].id]], "resolve_legacy_identifiers": True}]})
        self.assertEqual(result["analysis"]["included_customers"], 1)
        self.assertEqual(result["analysis"]["sources"][0]["identifier_resolution_rows"], {"phone": 1})
        self.assertNotIn(str(self.partners[2].id), json.dumps(result["rows"]))

    def test_basket_math_counts_documents_not_duplicate_lines(self):
        invoices = self._legacy_analysis_fixture()
        products = self.env["product.product"].create([{"name": "Copilot Basket A"}, {"name": "Copilot Basket B"}])
        line = self.env["legacy.invoice.line"]
        for i, (invoice, product) in enumerate([(invoices[0], products[0]), (invoices[0], products[0]),
                (invoices[0], products[1]), (invoices[1], products[0]), (invoices[2], products[1])]):
            line.create({"invoice_id": invoice.id, "source_id": 1000+i, "name": product.name, "product_id": product.id, "quantity": 1})
        result = self.query.analyze_baskets({"model": "legacy.invoice.line", "domain": [["invoice_id", "in", invoices.ids]],
            "transaction_field": "invoice_id", "product_field": "product_id"})
        self.assertEqual(result["analysis"]["sources"][0]["analyzed_baskets"], 3)
        self.assertEqual(result["rows"][0]["baskets_together"], 1)
        self.assertEqual(result["rows"][0]["support_pct"], 33.33)
        self.assertEqual(result["rows"][0]["a_to_b_pct"], 50)
        self.assertEqual(result["rows"][0]["lift"], .75)
        self.assertEqual(result["query"]["model"], "legacy.invoice.line")
        self.assertTrue(self.service.export_from_query_meta(result["query"], "xlsx")["file_id"])

    def test_public_web_isolated_payload_and_real_citation_validation(self):
        self.settings.openai_api_key = "fake-not-sent"
        payload = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text",
            "text": "A public fact. reference", "annotations": [{"type": "url_citation", "url": "https://www.apple.com/iphone/",
            "title": "Apple", "start_index": 15, "end_index": 24}]}]}]}
        with patch("odoo.addons.tradeline_ai_copilot.models.ai_copilot_research.requests.post") as post:
            post.return_value.iter_content.return_value = [json.dumps(payload).encode()]
            result = self.service._research_public_web("iPhone public product specifications", "product")
            request = post.call_args.kwargs["json"]
            self.assertFalse(request["store"])
            self.assertNotIn("Copilot Alpha", json.dumps(request))
            self.assertNotIn("conversation", request)
            self.assertEqual(request["tools"][0]["type"], "web_search")
            self.assertEqual(result["citations"][0]["id"], "W1")
            self.assertIn("[W1]", result["content"])
            self.assertTrue(post.return_value.close.called)
        for subject in ["contact person@example.com", "call 01012345678", "invoice S123 private details", "secret api key"]:
            with self.subTest(subject=subject), self.assertRaises(ValidationError):
                self.service._research_public_web(subject)

    def test_web_citations_are_preserved_without_fabricated_odoo_sources(self):
        web = {"kind": "web", "subject": "Public retail strategy", "content": "Fact [W1]", "queried_at": "2026-01-01",
            "scope": "Public research", "citations": [{"id": "W1", "title": "Official", "url": "https://www.apple.com/"}]}
        answer = self.service._finish_agent_response("Research publicly", False, "openai", "gpt-6.1-sol", "Fact [W1]", [web], [], True)
        self.assertTrue(any(b["type"] == "web_sources" for b in answer["blocks"]))
        self.assertFalse(any(b["type"] == "warning" for b in answer["blocks"]))

    def test_greetings_are_conversational_but_data_claims_still_need_queries(self):
        greeting = self.service._finish_agent_response("hello", False, "openai", "test", "Hello. What are we looking into?", [], [], True)
        self.assertFalse(any(b["type"] == "warning" for b in greeting["blocks"]))

    def test_clarification_ends_both_provider_loops_before_bundled_queries(self):
        question = {"question": "Which period and sales measure should I compare?", "choices": ["Posted invoices", "Confirmed orders"]}
        self.settings.openai_api_key = "fake-not-sent"
        claude = {"content": [
            {"type": "tool_use", "id": "q1", "name": "query_data", "input": self._spec()},
            {"type": "tool_use", "id": "c1", "name": "request_clarification", "input": question}]}
        openai = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "q1", "type": "function", "function": {"name": "query_data", "arguments": json.dumps(self._spec())}},
            {"id": "c1", "type": "function", "function": {"name": "request_clarification", "arguments": json.dumps(question)}}]}}]}
        for provider, payload in [("claude", claude), ("openai", openai)]:
            with self.subTest(provider=provider), patch.object(type(self.service), "_agent_request", return_value=payload) as request, \
                    patch.object(type(self.query), "execute_query", side_effect=AssertionError("Ambiguous scope must not query")):
                answer = self.service.generate_response("Compare our sales", provider=provider)
            self.assertEqual(request.call_count, 1)
            self.assertEqual(answer["blocks"][0]["content"], question["question"])
            self.assertFalse(any(b["type"] in {"table", "warning"} for b in answer["blocks"]))
            self.assertEqual(answer["blocks"][1], {"type": "reply_choices", "choices": question["choices"]})
        unsupported = self.service._finish_agent_response("Sales?", False, "claude", "test", "Sales were 999.", [], [], True)
        self.assertTrue(any("No Odoo data query" in b.get("content", "") for b in unsupported["blocks"]))

    def test_clarification_validation_and_arabic_question_persistence(self):
        for values in [{"question": "?"}, {"question": "Compare what?", "choices": "bad"},
                       {"question": "Compare what?", "choices": ["x" * 161]}, {"question": "Compare what?", "write": True}]:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.service._agent_dispatch("request_clarification", values, [], False, "claude", "test")
        question = {"question": "هل تقصد أوامر البيع أم الفواتير المرحلة؟", "choices": ["الفواتير المرحلة", "أوامر البيع"]}
        payload = {"content": [{"type": "tool_use", "id": "clarify", "name": "request_clarification", "input": question}]}
        with patch.object(type(self.service), "_agent_request", return_value=payload):
            result = self.env["ai.copilot.ui"].with_user(self.user).chat_send({"message": "قارن المبيعات", "provider": "claude"})
        saved = self.env["ai.copilot.message"].browse(result["message_id"])
        self.assertEqual(saved.content, question["question"])
        self.assertEqual(saved.response_json[1]["choices"], question["choices"])
        self.assertFalse(any(b["type"] == "warning" for b in saved.response_json))

    def test_sol_streamed_clarification_finishes_without_second_generation(self):
        self.settings.openai_api_key = "fake-not-sent"
        response = FakeProviderStream([{"type": "response.completed", "response": {"status": "completed", "output": [{
            "type": "function_call", "call_id": "c1", "name": "request_clarification", "status": "completed",
            "arguments": json.dumps({"question": "Which upgrade and purchase period should I use?", "choices": []})}]}}])
        with patch("odoo.addons.tradeline_ai_copilot.models.ai_copilot_agent.requests.post", return_value=response) as post:
            events = list(self.service._generate_response_stream("Who should we target?", provider="openai", llm_model="gpt-6.1-sol"))
        self.assertEqual(post.call_count, 1)
        final = events[-1]["response"]
        self.assertEqual(final["blocks"], [{"type": "text", "content": "Which upgrade and purchase period should I use?"}])
        self.assertTrue(response.closed)

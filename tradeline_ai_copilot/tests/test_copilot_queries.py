import base64
import io
import zipfile
import json
import time
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
        self.assertEqual({tool["name"] for tool in self.service._agent_tools()}, {"search_models", "describe_model", "query_data"})

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

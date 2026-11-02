import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from xmlrpc.client import Fault

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

from odoo_client import Config, OdooReportingClient, ReportingError, TOOL_NAMES
from server import build_server


class ConnectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.key = self.directory / "odoo.key"
        self.key.write_text("test-only-not-a-real-key", encoding="utf-8")
        self.path = self.directory / "config.json"
        self.raw = {"url": "https://example.odoo.com", "database": "test", "login": "reporter", "api_key_file": str(self.key)}
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.raw), encoding="utf-8")

    def test_secret_is_not_in_repr(self):
        self.assertNotIn("test-only-not-a-real-key", repr(Config.load(self.path)))

    def test_unsafe_urls_rejected(self):
        for url in ["http://example.com", "https://user:secret@example.com", "https://example.com/?key=x", "https://example.com/path", "https://example.com/#x"]:
            with self.subTest(url=url):
                self.raw["url"] = url
                self.save()
                with self.assertRaises(ReportingError):
                    Config.load(self.path)

    def test_inline_secret_and_unknown_config_rejected(self):
        self.raw["api_key"] = "secret"
        self.save()
        with self.assertRaises(ReportingError):
            Config.load(self.path)

    def test_relative_key_path_rejected(self):
        self.raw["api_key_file"] = "odoo.key"
        self.save()
        with self.assertRaises(ReportingError):
            Config.load(self.path)

    def test_unknown_tool_has_no_network_side_effect(self):
        with patch("odoo_client.ServerProxy") as proxy:
            with self.assertRaises(ReportingError):
                OdooReportingClient(self.path).call("write", {})
            proxy.assert_not_called()

    def test_oversized_and_nonfinite_arguments_rejected(self):
        for args in [{"x": "a" * 33000}, {"x": float("nan")}]:
            with self.assertRaises(ReportingError):
                OdooReportingClient(self.path).call("query", args)

    def test_rpc_can_only_call_fixed_bridge(self):
        common, objects = MagicMock(), MagicMock()
        common.__enter__.return_value = common
        objects.__enter__.return_value = objects
        common.authenticate.return_value = 27
        objects.execute_kw.return_value = {"read_only": True}
        with patch("odoo_client.ServerProxy", side_effect=[common, objects]):
            self.assertEqual(OdooReportingClient(self.path).call("connection_info", {}), {"read_only": True})
        args, kwargs = objects.execute_kw.call_args
        self.assertEqual(args[3:], ("tradeline.mcp.reporting", "execute_tool", ["connection_info", {}], {}))
        self.assertFalse(kwargs)

    def test_fault_does_not_disclose_traceback_or_key(self):
        common = MagicMock()
        common.__enter__.return_value = common
        common.authenticate.side_effect = Fault(1, "Traceback: password=test-only-not-a-real-key")
        with patch("odoo_client.ServerProxy", return_value=common):
            with self.assertRaises(ReportingError) as error:
                OdooReportingClient(self.path).call("connection_info", {})
        self.assertNotIn("test-only-not-a-real-key", str(error.exception))
        self.assertNotIn("Traceback", str(error.exception))

    def test_superuser_credentials_rejected(self):
        common = MagicMock()
        common.__enter__.return_value = common
        common.authenticate.return_value = 1
        with patch("odoo_client.ServerProxy", return_value=common) as proxy:
            with self.assertRaises(ReportingError):
                OdooReportingClient(self.path).call("connection_info", {})
            self.assertEqual(proxy.call_count, 1)

    def test_citation_links_use_configured_odoo_host(self):
        common, objects = MagicMock(), MagicMock()
        common.__enter__.return_value = common
        objects.__enter__.return_value = objects
        common.authenticate.return_value = 27
        objects.execute_kw.return_value = {"citation": {"path": "/tradeline/mcp/source?tool=query",
            "records": [{"id": 42, "path": "/odoo/res.partner/42"}]}}
        with patch("odoo_client.ServerProxy", side_effect=[common, objects]):
            result = OdooReportingClient(self.path).call("query", {"spec": {}})
        self.assertEqual(result["citation"]["url"], "https://example.odoo.com/tradeline/mcp/source?tool=query")
        self.assertEqual(result["citation"]["records"][0]["url"], "https://example.odoo.com/odoo/res.partner/42")

    def test_sdk_tool_schemas_and_annotations(self):
        class Stub:
            def call(self, name, arguments):
                return [] if name == "search_models" else {"tool": name}

        async def scenario():
            async with Client(build_server(Stub())) as client:
                listed = await client.list_tools()
                self.assertEqual({t.name for t in listed.tools}, TOOL_NAMES)
                for tool in listed.tools:
                    self.assertTrue(tool.annotations.read_only_hint)
                    self.assertFalse(tool.annotations.destructive_hint)
                    self.assertNotIn("api_key", json.dumps(tool.input_schema))
                response = await client.call_tool("query", {"spec": {"model": "res.partner", "fields": ["name"]}})
                self.assertFalse(response.is_error)
                self.assertEqual(response.structured_content, {"tool": "query"})
        asyncio.run(scenario())

    def test_stdio_discovery_without_credentials_and_safe_failure(self):
        async def scenario():
            params = StdioServerParameters(command=sys.executable, args=[str(Path(__file__).with_name("server.py")),
                "--config", str(self.directory / "missing.json")])
            async with Client(params, read_timeout_seconds=30) as client:
                tools = await client.list_tools()
                self.assertEqual(len(tools.tools), 12)
                response = await client.call_tool("connection_info", {})
                self.assertTrue(response.is_error)
                self.assertIn("configuration", str(response.content).lower())
        asyncio.run(scenario())

    def test_legacy_client_handshake_compatibility(self):
        async def scenario():
            params = StdioServerParameters(command=sys.executable, args=[str(Path(__file__).with_name("server.py")),
                "--config", str(self.directory / "missing.json")])
            async with Client(params, mode="legacy", read_timeout_seconds=30) as client:
                listed = await client.list_tools()
                self.assertEqual({t.name for t in listed.tools}, TOOL_NAMES)
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()

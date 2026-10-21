"""Small fixed RPC client. Credentials stay local and never enter tool schemas."""
from dataclasses import dataclass, field
import json
from pathlib import Path
import socket
import ssl
from urllib.parse import urlsplit
from xmlrpc.client import Fault, ProtocolError, SafeTransport, ServerProxy


TOOL_NAMES = frozenset({"connection_info", "search_models", "describe_model", "query", "search_products",
    "product_sales_units", "metric_definitions", "financial_report", "data_freshness", "customer_sets", "analyze_baskets"})


class ReportingError(Exception):
    """Safe, credential-free error suitable for an MCP response."""


@dataclass(frozen=True)
class Config:
    url: str
    database: str
    login: str
    api_key: str = field(repr=False)

    @classmethod
    def load(cls, path: Path):
        try:
            if path.stat().st_size > 8192:
                raise ReportingError("Local configuration is too large.")
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or set(raw) != {"url", "database", "login", "api_key_file"}:
                raise ReportingError("Use the four fields in config.example.json; inline credentials are not supported.")
            if any(not isinstance(v, str) or not v.strip() for v in raw.values()):
                raise ReportingError("Complete every local configuration field.")
            parsed = urlsplit(raw["url"])
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                    or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
                raise ReportingError("Use the HTTPS Odoo base URL without credentials, path, query or fragment.")
            key_path = Path(raw["api_key_file"]).expanduser()
            if not key_path.is_absolute():
                raise ReportingError("Use an absolute path for the local API-key file.")
            if key_path.stat().st_size > 4096:
                raise ReportingError("The local API-key file is invalid.")
            key = key_path.read_text(encoding="utf-8").strip()
            if not key or any(c.isspace() for c in key):
                raise ReportingError("The API-key file must contain one nonempty key.")
            return cls(raw["url"].rstrip("/"), raw["database"], raw["login"], key)
        except (OSError, ValueError, TypeError) as exc:
            raise ReportingError("Local configuration is missing or invalid. Follow the setup guide; do not paste credentials into chat.") from None


class TimeoutTransport(SafeTransport):
    def __init__(self):
        super().__init__(context=ssl.create_default_context())

    def make_connection(self, host):
        connection = super().make_connection(host)
        connection.timeout = 60
        return connection


class OdooReportingClient:
    def __init__(self, config_path: Path):
        self.config_path = config_path

    def call(self, tool: str, arguments: dict):
        if tool not in TOOL_NAMES or not isinstance(arguments, dict):
            raise ReportingError("Unsupported reporting tool.")
        try:
            encoded = json.dumps(arguments, allow_nan=False)
        except (ValueError, TypeError, RecursionError):
            raise ReportingError("Use finite JSON tool arguments.") from None
        if len(encoded.encode("utf-8")) > 32768:
            raise ReportingError("Narrow the request to 32 KiB of arguments.")
        config = Config.load(self.config_path)
        try:
            # A fresh proxy per call avoids sharing a transport across MCP worker
            # threads. XML-RPC does not follow redirects; TLS verification stays on.
            with ServerProxy(config.url + "/xmlrpc/2/common", transport=TimeoutTransport(), allow_none=True) as common:
                uid = common.authenticate(config.database, config.login, config.api_key, {})
            if type(uid) is not int or uid <= 1:
                raise ReportingError("Odoo authentication failed or a superuser was supplied. Use an approved named user.")
            with ServerProxy(config.url + "/xmlrpc/2/object", transport=TimeoutTransport(), allow_none=True) as objects:
                result = objects.execute_kw(config.database, uid, config.api_key,
                    "tradeline.mcp.reporting", "execute_tool", [tool, arguments], {})
            if not isinstance(result, (dict, list)):
                raise ReportingError("Odoo returned an unsupported reporting response.")
            if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 524288:
                raise ReportingError("Result is too large. Reduce the limit or narrow the scope.")
            return result
        except Fault:
            # An Odoo XML-RPC fault contains a full server traceback. Never echo it.
            raise ReportingError("Odoo refused this report. Check MCP activation, user/group access, approved models/fields and query syntax; an unavailable source is not zero sales.") from None
        except (ProtocolError, OSError, socket.timeout, ssl.SSLError, ValueError, TypeError):
            raise ReportingError("The secure Odoo connection could not complete. No write was requested; check connectivity and the administrator's setup.") from None

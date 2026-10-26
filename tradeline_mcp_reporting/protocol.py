"""Stateless JSON-RPC subset of MCP Streamable HTTP. No Odoo/SDK dependencies."""
import copy
import json
from pathlib import Path

ROOT = Path(__file__).parent
CATALOG = json.loads((ROOT / "tool_catalog.json").read_text(encoding="utf-8"))
CATALOG.append({"name": "repeat_customers", "description": "Complete phone-identity repeat-purchase report. spec: years=[2025,2026], company_id from connection_info, optional as_of_date YYYY-MM-DD. Separates two purchase dates, two documents and cross-year customers; returns coverage and no raw phones. Bounded read-only snapshot; if the full scan exceeds safeguards, do not infer a total from partial rows.",
    "inputSchema": {"type": "object", "properties": {"spec": {"type": "object"}}, "required": ["spec"], "additionalProperties": False},
    "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}})
INSTRUCTIONS = (ROOT / "instructions.txt").read_text(encoding="utf-8")
CATALOG.append({
    "name": "live_shopify",
    "description": "Separately enabled, administrator-only LIVE Shopify order reader using existing store credentials. Start operation=stores, then connection with instance_id; report orders_summary or orders with exact date_from/date_to (order CREATED dates in shop timezone). Optional financial_status, fulfillment_status, cancelled/test_orders (any/yes/no), grain total/day/month, limit up to 500. All statuses are included unless explicitly filtered. Fixed GraphQL reads only, no sync/import/refresh/mutations, customer details, products or inventory. Store-wide access is not Odoo branch-scoped. Complete bounded pagination (2000 orders max); failures are unavailable, not zero. Current tax-inclusive order value is not recognized revenue or margin; do not add it to Odoo invoices.",
    "inputSchema": {"type": "object", "properties": {"spec": {
        "type": "object", "properties": {
            "operation": {"type": "string", "enum": ["stores", "connection", "orders_summary", "orders"]},
            "instance_id": {"type": "integer", "minimum": 1},
            "date_from": {"type": "string", "format": "date"},
            "date_to": {"type": "string", "format": "date"},
            "financial_status": {"type": "string", "enum": ["any", "paid", "pending", "authorized", "partially_paid", "partially_refunded", "refunded", "voided"]},
            "fulfillment_status": {"type": "string", "enum": ["any", "fulfilled", "unfulfilled", "partial"]},
            "cancelled": {"type": "string", "enum": ["any", "yes", "no"]},
            "test_orders": {"type": "string", "enum": ["any", "yes", "no"]},
            "grain": {"type": "string", "enum": ["total", "day", "month"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            "title": {"type": "string"}}, "additionalProperties": False}},
        "required": ["spec"], "additionalProperties": False},
    "annotations": {"readOnlyHint": True, "destructiveHint": False,
                    "idempotentHint": True, "openWorldHint": True}})
VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}


def error(ident, code, message):
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}


def absolute_citations(value, base):
    if isinstance(value, dict):
        for key, item in list(value.items()):
            if key == "path" and isinstance(item, str) and item.startswith(("/odoo/", "/tradeline/mcp/source?")):
                value["url"] = base + item
            else:
                absolute_citations(item, base)
    elif isinstance(value, list):
        for item in value:
            absolute_citations(item, base)


def handle_message(message, call, base):
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return error(None, -32600, "Invalid JSON-RPC request; batches are not supported."), 400
    ident = message.get("id")
    if "id" in message and (type(ident) not in {str, int} or (isinstance(ident, str) and len(ident) > 200)):
        return error(None, -32600, "Invalid request ID."), 400
    method = message["method"]
    if "id" not in message:
        if method in {"notifications/initialized", "notifications/cancelled"}:
            return None, 202
        return None, 400
    params = message.get("params", {})
    if not isinstance(params, dict):
        return error(ident, -32602, "Parameters must be an object."), 200
    if method == "initialize":
        requested = params.get("protocolVersion")
        if not isinstance(requested, str) or not isinstance(params.get("clientInfo"), dict) or not isinstance(params.get("capabilities"), dict):
            return error(ident, -32602, "Initialization requires protocolVersion, clientInfo and capabilities."), 200
        result = {"protocolVersion": requested if requested in VERSIONS else "2025-11-25",
            "serverInfo": {"name": "Tradeline Odoo reporting", "version": "0.5.0"},
            "capabilities": {"tools": {}}, "instructions": INSTRUCTIONS}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        if params.get("cursor"):
            return error(ident, -32602, "This catalogue has no further pages."), 200
        result = {"tools": copy.deepcopy(CATALOG)}
    elif method == "tools/call":
        name, args = params.get("name"), params.get("arguments", {})
        tool = next((t for t in CATALOG if t["name"] == name), None)
        if not tool:
            return error(ident, -32602, "Unknown reporting tool."), 200
        schema = tool["inputSchema"]
        allowed, required = set(schema.get("properties", {})), set(schema.get("required", []))
        if not isinstance(args, dict) or set(args) - allowed or not required <= args.keys():
            return error(ident, -32602, "Unsupported or missing tool arguments."), 200
        try:
            data = call(name, args)
            structured = data if isinstance(data, dict) else {"result": data}
            absolute_citations(structured, base)
            text = json.dumps(structured, ensure_ascii=False, allow_nan=False)
            if len(text.encode("utf-8")) > 524288:
                raise ValueError("Result exceeds the reporting budget; narrow the request.")
            result = {"content": [{"type": "text", "text": text}], "structuredContent": structured, "isError": False}
        except ValueError as exc:
            result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    elif method in {"resources/list", "prompts/list"}:
        result = {"resources" if method.startswith("resources/") else "prompts": []}
    else:
        return error(ident, -32601, "Method not found."), 200
    return {"jsonrpc": "2.0", "id": ident, "result": result}, 200

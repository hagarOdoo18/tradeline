"""One fixed RPC entry point. No arbitrary methods, code, SQL, exports or writes."""
import json
from urllib.parse import urlencode

from psycopg2 import Error as DatabaseError

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools.json import json_default
from .scope import scope_manifest


# Only these known reporting methods can cross the boundary. Never dispatch a
# model/method supplied by the client, or accept an RPC context from the model.
TOOLS = {
    "connection_info": (None, set(), set()),
    "reporting_coverage": (None, {"keyword", "offset"}, set()),
    "search_models": ("search_models", {"keyword", "offset"}, set()),
    "describe_model": ("describe_model", {"model"}, {"model"}),
    "query": ("execute_query", {"spec"}, {"spec"}),
    "search_products": ("search_products", {"search", "limit"}, {"search"}),
    "product_sales_units": ("product_sales_units", {"spec"}, {"spec"}),
    "metric_definitions": ("metric_definitions", set(), set()),
    "financial_report": ("financial_report", {"spec"}, {"spec"}),
    "data_freshness": ("data_freshness", {"spec"}, {"spec"}),
    "customer_sets": ("compare_customer_sets", {"spec"}, {"spec"}),
    "analyze_baskets": ("analyze_baskets", {"spec"}, {"spec"}),
}


class MCPReportingBridge(models.AbstractModel):
    _name = "tradeline.mcp.reporting"
    _description = "Opt-in Read-only MCP Reporting Bridge"

    def _reporting_company_ids(self, user):
        # Match the named user's assigned company access, preserving their main
        # company for company-dependent fields. Never accept client company IDs.
        main = user.company_id.id
        return [main] + sorted(set(user.company_ids.ids) - {main})

    def _assert_mcp_access(self):
        if self.env.is_superuser() or not self.env.user.has_group("tradeline_mcp_reporting.group_mcp_reporting_user"):
            raise AccessError("An approved MCP Reporting User is required; superuser access is not supported.")
        self.env["ai.copilot.service"]._assert_internal_user()
        settings = self.env["ai.copilot.settings"].sudo().search([], limit=1)
        if not settings or not settings.enable_mcp_reporting:
            raise AccessError("MCP reporting is disabled by the administrator.")

    @api.model
    def execute_tool(self, tool, arguments):
        self._assert_mcp_access()
        if not isinstance(tool, str) or tool not in TOOLS or not isinstance(arguments, dict):
            raise ValidationError("Choose a supported reporting tool and an argument object.")
        unused_method, allowed, required = TOOLS[tool]
        if set(arguments) - allowed or not required <= arguments.keys():
            raise ValidationError("Unsupported or missing tool arguments.")
        try:
            encoded = json.dumps(arguments, allow_nan=False)
        except (ValueError, TypeError, RecursionError) as exc:
            raise ValidationError("Tool arguments must be finite JSON values.") from exc
        if len(encoded.encode("utf-8")) > 32768:
            raise ValidationError("Narrow the request to at most 32 KiB of arguments.")

        # Authentication may already have queried the RPC cursor. Use a NEW
        # transaction, set read-only before its first query, and always roll it
        # back. Even an unexpected write in a custom model fails at PostgreSQL.
        try:
            with self.env.registry.cursor() as cr:
                cr.execute("SET TRANSACTION READ ONLY")
                cr.execute("SET LOCAL statement_timeout = '8000ms'")
                try:
                    clean = api.Environment(cr, self.env.uid, {}, su=False)
                    user = clean.user
                    clean = api.Environment(cr, self.env.uid, {
                        "allowed_company_ids": self._reporting_company_ids(user),
                        "lang": user.lang or "en_US", "tz": user.tz or "UTC",
                        "tradeline_mcp_reporting": True,
                    }, su=False)
                    bridge = clean[self._name]
                    bridge._assert_mcp_access()  # Recheck gates inside the snapshot.
                    result = bridge._dispatch(tool, arguments)
                    result = bridge._attach_citation(result, tool, arguments)
                    payload = json.loads(json.dumps(result, default=json_default, allow_nan=False))
                    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > 524288:
                        raise ValidationError("Result exceeds 512 KiB. Narrow the query or reduce its limit.")
                    return payload
                finally:
                    cr.rollback()
        except DatabaseError as exc:
            # Never expose SQL/tracebacks or connection details through XML-RPC.
            raise UserError("Reporting could not complete within the read-only database safeguards. Narrow the request or ask the administrator to review it.") from exc

    def _dispatch(self, tool, arguments):
        if tool == "reporting_coverage":
            return self._reporting_coverage(**arguments)
        if tool == "connection_info":
            return {"schema_version": 1, "read_only": True, "user": self.env.user.name,
                    "company_scope": [{"id": c.id, "name": c.name} for c in self.env.companies],
                    "queried_at_utc": fields.Datetime.to_string(fields.Datetime.now()),
                    "scope": "Only explicitly MCP-approved models/fields, within this Odoo user's ACLs and record rules. All assigned companies; main company retained as default. Filter company_id for a narrower business scope.",
                    "limitations": ["No raw SQL, arbitrary RPC, business changes, chat history, credentials or attachments.",
                                    "No live Shopify API or original Odoo 12 connection. Approved imported/saved datasets only.",
                                    "A query timestamp does not prove source completeness. Check data_freshness before historic claims."]}
        method = TOOLS[tool][0]
        return getattr(self.env["ai.copilot.query"], method)(**arguments)

    def _reporting_coverage(self, keyword="", offset=0):
        if not isinstance(keyword, str) or len(keyword) > 100 or type(offset) is not int or not 0 <= offset <= 10000:
            raise ValidationError("Use a keyword up to 100 characters and offset from 0 to 10,000.")
        entries = scope_manifest(self.env, self.env.user, include_fields=False)
        for entry in entries:
            entry.pop("fields")  # Field discovery remains describe_model's job.
            if not entry["reason"]:
                entry["reason"] = None if entry["mcp_enabled"] else "Not approved in this MCP scope snapshot"
            entry["available"] = not entry["reason"]
        matches = [e for e in entries if keyword.lower() in
                   (e["model"] + " " + e["label"] + " " + " ".join(e["modules"])).lower()]
        installed = self.env["ir.module.module"].sudo().search([("state", "=", "installed")])
        return {"installed_module_count": len(installed), "business_dataset_count": len(entries),
                "available_dataset_count": sum(e["available"] for e in entries),
                "matching_datasets": len(matches), "offset": offset, "datasets": matches[offset:offset + 60],
                "next_offset": offset + 60 if offset + 60 < len(matches) else None,
                "scope_note": "A module can contribute several datasets or only UI/technical functionality. Available means approved and readable, not populated or historically complete. Discover fields before querying.",
                "external_sources": {"original_odoo12": "Not connected", "live_shopify_api": "Not connected; saved Odoo Shopify datasets only"}}

    def _attach_citation(self, result, tool, arguments):
        if not isinstance(result, dict) or not (result.get("provenance") or tool == "metric_definitions"):
            return result
        encoded = urlencode({"tool": tool, "arguments": json.dumps(arguments, separators=(",", ":"), default=json_default)})
        citation = {"label": result.get("title") or result.get("source") or "Odoo reporting definitions",
                    "queried_at_utc": fields.Datetime.to_string(fields.Datetime.now()),
                    "note": "Opening the source reruns the same read-only report under the signed-in Odoo user's permissions; records can change after this answer."}
        if len(encoded) <= 6000:
            citation["path"] = "/tradeline/mcp/source?" + encoded
        else:
            citation["note"] += " The query is too large for a source link; retain the returned filters as the citation."
        rows = result.get("rows", [])
        model = result.get("source")
        # Only explicit visible IDs; grouped IDs or analysis customer IDs are not
        # assumed to identify the result's source model.
        if tool == "query" and model and "id" in result.get("columns", []):
            citation["records"] = [{"id": row["id"], "path": "/odoo/%s/%s" % (model, row["id"])}
                                   for row in rows[:10] if type(row.get("id")) is int and row["id"] > 0]
        result["citation"] = citation
        return result

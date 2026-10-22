"""Authenticated, read-only source receipts. No assets or business UI patches."""
import json
from html import escape

from odoo import http
from odoo.http import request
from odoo.exceptions import AccessError, UserError, ValidationError


class MCPSource(http.Controller):
    @http.route("/tradeline/mcp/source", type="http", auth="user", methods=["GET"], readonly=True)
    def source(self, tool="", arguments="{}", **unused):
        headers = [("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
                   ("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'self'")]
        try:
            if len(arguments.encode("utf-8")) > 32768:
                raise ValidationError("Source request is too large.")
            data = request.env["tradeline.mcp.reporting"].execute_tool(tool, json.loads(arguments))
        except (AccessError, UserError, ValidationError, ValueError, TypeError):
            return request.make_response("This Odoo source is unavailable to the signed-in user or the report could not complete. It is not evidence of zero records.", headers=headers, status=403)
        title = data.get("title", data.get("source", "Odoo reporting source")) if isinstance(data, dict) else "Odoo reporting source"
        content = "<h1>%s</h1><p>Read-only source receipt. This page refreshed the report using your current Odoo permissions. Values may differ from an earlier answer.</p>" % escape(str(title))
        if isinstance(data, dict) and "columns" in data and "rows" in data:
            columns, labels = data["columns"], data.get("labels", {})
            content += "<table><thead><tr>" + "".join("<th>%s</th>" % escape(str(labels.get(c, c))) for c in columns) + "</tr></thead><tbody>"
            for row in data["rows"]:
                content += "<tr>" + "".join("<td>%s</td>" % escape(str(row.get(c, ""))) for c in columns) + "</tr>"
            content += "</tbody></table>"
        content += "<h2>Report, definitions, filters and coverage</h2><pre>%s</pre>" % escape(json.dumps(data, ensure_ascii=False, indent=2))
        html = "<!doctype html><html><head><meta charset='utf-8'><title>Odoo reporting source</title><style>body{font:16px system-ui;margin:40px;color:#183b33}table{border-collapse:collapse}td,th{border:1px solid #ccd8d4;padding:8px;text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f7f5;padding:20px}</style></head><body>" + content + "</body></html>"
        return request.make_response(html, headers=headers + [("Content-Type", "text/html; charset=utf-8")])

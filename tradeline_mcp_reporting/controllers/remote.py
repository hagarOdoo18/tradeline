"""Stateless Streamable HTTP MCP and narrow OAuth routes; no business UI hooks."""
import json
from html import escape
from urllib.parse import urlencode, urlsplit

from odoo import http, tools
from odoo.http import request
from odoo.exceptions import AccessError, UserError, ValidationError
from ..oauth_utils import SCOPE, CLAUDE_CALLBACKS, canonical_url
from ..protocol import handle_message


def base_url():
    value = request.env["ir.config_parameter"].sudo().get_param("web.base.url", "")
    base = canonical_url(value, local_test=bool(tools.config["test_enable"]))
    # Never derive issuer/resource/redirects from an untrusted Host header.
    if request.httprequest.host.lower() != urlsplit(base).netloc.lower():
        raise AccessError("Use the canonical Odoo address.")
    if base.startswith("https://") and not request.httprequest.is_secure:
        raise AccessError("HTTPS is required for hosted reporting.")
    return base


def json_response(data, status=200, headers=()):
    return request.make_response(json.dumps(data, ensure_ascii=False, allow_nan=False), status=status,
        headers=[("Content-Type", "application/json"), ("Cache-Control", "no-store"),
                 ("X-Content-Type-Options", "nosniff"), *headers])


def bounded_json():
    # Odoo's wrapper exposes data, not Werkzeug's raw stream. The route's
    # max_content_length bounds reading before dispatch (including chunked input).
    raw = request.httprequest.data
    if len(raw) > 65536:
        raise ValueError("Request exceeds 64 KiB.")
    if request.httprequest.mimetype != "application/json":
        raise ValueError("Use application/json.")
    return json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))


def form_values():
    if request.httprequest.content_length is None or request.httprequest.content_length > 16384:
        raise ValueError("Invalid form size.")
    form = request.httprequest.form
    if any(len(form.getlist(key)) != 1 for key in form):
        raise ValueError("Duplicate form parameters.")
    return form.to_dict()


class MCPRemote(http.Controller):
    @http.route("/tradeline/mcp", type="http", auth="public", methods=["POST", "GET", "DELETE"],
                csrf=False, save_session=False, readonly=True, max_content_length=65536)
    def mcp(self, **unused):
        try:
            base = base_url()
            resource = base + "/tradeline/mcp"
            if request.httprequest.args:
                raise AccessError("MCP credentials belong only in the Authorization header.")
            # Cookies never substitute for bearer authentication on this route.
            auth = request.httprequest.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                raise AccessError("Reporting authentication required.")
            uid = request.env["tradeline.mcp.oauth.grant"]._authenticate(auth[7:], resource)
        except (AccessError, ValueError):
            # Do not reflect request Host into the authentication challenge.
            try:
                base = canonical_url(request.env["ir.config_parameter"].sudo().get_param("web.base.url", ""), bool(tools.config["test_enable"]))
                challenge = 'Bearer resource_metadata="%s/.well-known/oauth-protected-resource/tradeline/mcp", scope="%s"' % (base, SCOPE)
            except ValueError:
                challenge = "Bearer"
            return json_response({"error": "unauthorized"}, 401, [("WWW-Authenticate", challenge)])
        if request.httprequest.method != "POST":
            return json_response({"error": "Stateless MCP accepts POST requests."}, 405, [("Allow", "POST")])
        origin = request.httprequest.headers.get("Origin")
        if origin and origin not in {base, "https://claude.ai", "https://claude.com"}:
            return json_response({"error": "Invalid Origin."}, 403)
        version = request.httprequest.headers.get("MCP-Protocol-Version")
        if version and version not in {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}:
            return json_response({"error": "Unsupported MCP protocol version."}, 400)
        try:
            message = bounded_json()
        except (ValueError, TypeError, RecursionError):
            return json_response({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON request."}}, 400)
        bridge = request.env["tradeline.mcp.reporting"].with_user(uid).sudo(False)

        def call(name, arguments):
            try:
                result = bridge.execute_tool(name, arguments)
                if name == "connection_info":
                    result.update(connector_transport="Odoo-hosted HTTPS", cross_device_access=True)
                return result
            except (AccessError, UserError, ValidationError):
                # No SQL, tracebacks, secrets or unexpected record content.
                raise ValueError("This report is unavailable or could not complete within its safeguards. Narrow the request or review dataset/field permissions; unavailable is not zero.") from None

        response, status = handle_message(message, call, base)
        if response is None:
            return request.make_response("", status=status, headers=[("Cache-Control", "no-store")])
        return json_response(response, status)

    @http.route(["/.well-known/oauth-protected-resource/tradeline/mcp", "/tradeline/mcp/.well-known/oauth-protected-resource"],
                type="http", auth="public", methods=["GET"], save_session=False, readonly=True)
    def resource_metadata(self, **unused):
        try:
            base = base_url()
        except (AccessError, ValueError):
            return json_response({"error": "Canonical HTTPS Odoo URL required."}, 400)
        return json_response({"resource": base + "/tradeline/mcp", "resource_name": "Tradeline read-only Odoo reporting",
            "authorization_servers": [base + "/tradeline/mcp/oauth"], "scopes_supported": [SCOPE],
            "bearer_methods_supported": ["header"]})

    @http.route(["/.well-known/oauth-authorization-server/tradeline/mcp/oauth", "/tradeline/mcp/oauth/.well-known/oauth-authorization-server"],
                type="http", auth="public", methods=["GET"], save_session=False, readonly=True)
    def authorization_metadata(self, **unused):
        try:
            base = base_url() + "/tradeline/mcp/oauth"
        except (AccessError, ValueError):
            return json_response({"error": "Canonical HTTPS Odoo URL required."}, 400)
        return json_response({"issuer": base, "authorization_endpoint": base + "/authorize", "token_endpoint": base + "/token",
            "registration_endpoint": base + "/register", "revocation_endpoint": base + "/revoke",
            "scopes_supported": [SCOPE], "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none"], "code_challenge_methods_supported": ["S256"]})

    @http.route("/tradeline/mcp/oauth/register", type="http", auth="public", methods=["POST"], csrf=False, save_session=False, max_content_length=65536)
    def register(self, **unused):
        try:
            base_url()
            request.env["tradeline.mcp.oauth.grant"]._check_enabled()
            result = request.env["tradeline.mcp.oauth.client"]._register_client(bounded_json())
            return json_response(result, 201)
        except (AccessError, ValueError, TypeError):
            return json_response({"error": "invalid_client_metadata", "error_description": "Hosted reporting must be enabled. Use Claude's HTTPS callback and Register automatically (public client + PKCE)."}, 400)

    @http.route("/tradeline/mcp/oauth/authorize", type="http", auth="user", methods=["GET", "POST"], csrf=True)
    def authorize(self, **unused):
        grant = request.env["tradeline.mcp.oauth.grant"]
        try:
            base = base_url()
            if request.httprequest.method == "GET":
                args = request.httprequest.args
                if any(len(args.getlist(key)) != 1 for key in args):
                    raise ValueError("Duplicate parameters.")
                params = args.to_dict()
            else:
                params = form_values()
                action = params.pop("decision", "")
                params.pop("csrf_token", None)
                if action not in {"approve", "deny"}:
                    raise ValueError("Choose an authorization decision.")
            grant._check_user(request.env.uid)
            client = grant._authorize_request(params, base + "/tradeline/mcp")
            if request.httprequest.method == "POST":
                target = params["redirect_uri"]
                result = {"state": params["state"], "iss": base + "/tradeline/mcp/oauth"}
                if action == "deny":
                    result["error"] = "access_denied"
                else:
                    result["code"] = grant._issue_code(params, base + "/tradeline/mcp", request.env.uid)
                return request.redirect(target + "?" + urlencode(result), code=303, local=False)
            inputs = "".join('<input type="hidden" name="%s" value="%s">' % (escape(k, quote=True), escape(v, quote=True)) for k, v in params.items())
            inputs += '<input type="hidden" name="csrf_token" value="%s">' % escape(request.csrf_token(), quote=True)
            companies = ", ".join(request.env.user.company_ids.mapped("name"))
            html = '''<!doctype html><html><head><meta charset="utf-8"><title>Connect Claude to Odoo</title>
                <style>body{font:17px system-ui;max-width:650px;margin:60px auto;padding:24px;color:#173d34}button{font:inherit;padding:12px 20px;margin-right:12px;cursor:pointer}strong{color:#145c47}p{line-height:1.6}</style></head><body>
                <h1>Connect Claude to Odoo reporting</h1><p>Signed in as <strong>%s</strong>.</p>
                <p>Allow Claude to read your approved reporting datasets for: <strong>%s</strong>.</p>
                <p>Requested Odoo data will be sent to the Claude account completing this connection. Existing company, record and field permissions apply. Business records cannot be created, edited or deleted through this connector.</p>
                <p>Return address: <strong>%s</strong>. Client label: %s.</p>
                <form method="post" action="/tradeline/mcp/oauth/authorize">%s
                <button name="decision" value="approve">Allow read-only reporting</button><button name="decision" value="deny">Cancel</button></form>
                <p>You can revoke this connection at <a href="/tradeline/mcp/connections">Odoo → Reporting connections</a>.</p></body></html>''' % (
                    escape(request.env.user.name), escape(companies), escape(urlsplit(params["redirect_uri"]).hostname), escape(client.client_name), inputs)
            return request.make_response(html, headers=consent_headers(allow_claude_callback=True))
        except (AccessError, ValueError, TypeError):
            return request.make_response("This reporting sign-in is invalid, disabled, or unavailable to this user. Restart the connection from Claude.", status=400, headers=consent_headers())

    @http.route("/tradeline/mcp/oauth/token", type="http", auth="public", methods=["POST"], csrf=False, save_session=False, max_content_length=16384)
    def token(self, **unused):
        try:
            base = base_url()
            params = form_values()
            if set(params) - {"grant_type", "client_id", "code", "code_verifier", "redirect_uri", "resource", "refresh_token", "scope"}:
                raise ValueError("Unsupported token parameters.")
            if params.get("scope", SCOPE) != SCOPE:
                raise ValueError("Invalid scope.")
            result = request.env["tradeline.mcp.oauth.grant"]._exchange(params, base + "/tradeline/mcp")
            return json_response(result)
        except (AccessError, ValueError, TypeError):
            # Catch in this transaction so refresh-token replay revocation commits.
            return json_response({"error": "invalid_grant", "error_description": "Sign-in expired, invalid or revoked. Reconnect in Claude."}, 400)

    @http.route("/tradeline/mcp/oauth/revoke", type="http", auth="public", methods=["POST"], csrf=False, save_session=False, max_content_length=16384)
    def revoke(self, **unused):
        try:
            base_url()
            params = form_values()
            request.env["tradeline.mcp.oauth.grant"]._revoke_token(params.get("token"), params.get("client_id"))
        except (AccessError, ValueError, TypeError):
            pass
        return json_response({})

    @http.route("/tradeline/mcp/connections", type="http", auth="user", methods=["GET", "POST"], csrf=True)
    def connections(self, **unused):
        # Can always disconnect, even after the global reporting switch is off.
        grants = request.env["tradeline.mcp.oauth.grant"].sudo().search([("user_id", "=", request.env.uid), ("kind", "=", "token"), ("revoked", "=", False)])
        if request.httprequest.method == "POST":
            grants.write({"revoked": True})
            return request.redirect("/tradeline/mcp/connections", code=303)
        content = '<!doctype html><html><head><meta charset="utf-8"><title>Reporting connections</title></head><body><h1>Reporting connections</h1><p>%s active grants for %s.</p><form method="post"><input type="hidden" name="csrf_token" value="%s"><button>Disconnect all my reporting connections</button></form></body></html>' % (len(grants), escape(request.env.user.name), escape(request.csrf_token(), quote=True))
        return request.make_response(content, headers=consent_headers())


def consent_headers(allow_claude_callback=False):
    # Chrome/Safari apply form-action to the POST's 303 redirect too. Only the
    # already allowlisted Claude callback may receive the authorization response.
    # The disconnect/error pages retain self-only form submission.
    form_action = "'self'"
    if allow_claude_callback:
        form_action += " " + " ".join(sorted(CLAUDE_CALLBACKS))
    return [("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store"),
            ("Referrer-Policy", "no-referrer"), ("X-Content-Type-Options", "nosniff"),
            ("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action %s; frame-ancestors 'none'" % form_action)]

"""Real local HTTP → OAuth → MCP → read-only PostgreSQL integration check.

Run through Odoo shell against a running local QA server on 127.0.0.1:8078.
Refuses production. Disposable synthetic fixtures/configuration restored finally.
Credentials exist only in this process memory and are never printed/saved.
"""
import json
import secrets
from urllib.parse import parse_qs, urlencode, urlsplit

import requests
from lxml import html
from odoo import http
from ..oauth_utils import SCOPE, challenge


def run(env):
    if not env.cr.dbname.startswith("codex_copilot_qa_"):
        raise RuntimeError("Local isolated QA database required.")
    base = "http://127.0.0.1:8078"
    resource = base + "/tradeline/mcp"
    settings = env["ai.copilot.settings"].sudo().get_singleton()
    old_settings = {key: settings[key] for key in ("enable_mcp_reporting", "enable_mcp_remote")}
    icp = env["ir.config_parameter"].sudo()
    old_base = icp.get_param("web.base.url")
    policy = env["ai.copilot.allowed.model"].sudo().search([("model_name", "=", "res.partner")], limit=1)
    old_policy = {key: policy[key] for key in ("enabled", "mcp_enabled", "mcp_fields_csv", "allowed_fields_csv")}
    user = profile = visible = hidden = rule = client = None
    local_session = None
    session = requests.Session()
    session.trust_env = False
    checks = []
    try:
        password = secrets.token_urlsafe(32)
        user = env["res.users"].with_context(no_reset_password=True).create({"name": "Hosted HTTP local QA",
            "login": "hosted_http_disposable_qa", "password": password,
            "company_id": env.company.id, "company_ids": [(6, 0, env.company.ids)],
            "groups_id": [(6, 0, env.ref("tradeline_mcp_reporting.group_mcp_reporting_user").ids)]})
        profile = user.partner_id
        visible = env["res.partner"].create({"name": "Hosted visible QA", "company_id": env.company.id})
        hidden = env["res.partner"].create({"name": "Hosted hidden QA", "company_id": env.company.id})
        rule = env["ir.rule"].create({"name": "Hosted disposable QA rule", "model_id": env.ref("base.model_res_partner").id,
            "domain_force": "[('id','!=',%d)] if user.id == %d else [(1,'=',1)]" % (hidden.id, user.id)})
        settings.write({"enable_mcp_reporting": True, "enable_mcp_remote": True})
        policy.write({"enabled": True, "mcp_enabled": True, "mcp_fields_csv": "id,name", "allowed_fields_csv": False})
        icp.set_param("web.base.url", base)
        env.cr.commit()
        response = session.get(resource, timeout=30)
        assert response.status_code == 401 and "resource_metadata=" in response.headers["WWW-Authenticate"]
        metadata = session.get(base + "/.well-known/oauth-protected-resource/tradeline/mcp", timeout=30).json()
        assert metadata["resource"] == resource
        issuer = metadata["authorization_servers"][0]
        discovery = session.get(base + "/.well-known/oauth-authorization-server/tradeline/mcp/oauth", timeout=30).json()
        assert discovery["issuer"] == issuer and discovery["code_challenge_methods_supported"] == ["S256"]
        checks.append("real HTTP protected-resource and authorization discovery")
        response = session.post(discovery["registration_endpoint"], json={"client_name": "Claude local QA " + secrets.token_hex(6),
            "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]}, timeout=30)
        assert response.status_code == 201, response.status_code
        registered = response.json()
        client = env["tradeline.mcp.oauth.client"].sudo().search([("client_id", "=", registered["client_id"])])
        env.cr.commit()  # Authentication uses its own transactions.
        verifier = secrets.token_urlsafe(48)
        auth_args = {"client_id": registered["client_id"], "response_type": "code", "scope": SCOPE,
            "redirect_uri": registered["redirect_uris"][0], "resource": resource, "state": "local-qa-state",
            "code_challenge": challenge(verifier), "code_challenge_method": "S256"}
        # Use Odoo's normal password authentication and session store. Skip the
        # unrelated full web-client menu/icon payload (the QA clone lacks icons).
        local_session = http.root.session_store.new()
        logged_in = env["res.users"].authenticate(env.cr.dbname, {"type": "password", "login": user.login, "password": password},
            {"interactive": True, "base_location": base, "HTTP_HOST": "127.0.0.1:8078", "REMOTE_ADDR": "127.0.0.1"})
        assert logged_in["uid"] == user.id
        local_session.pre_uid, local_session.pre_login = user.id, user.login
        local_session.finalize(env)
        assert local_session.uid == user.id
        http.root.session_store.save(local_session)
        session.cookies.set("session_id", local_session.sid)
        # A valid signed-in Odoo browser session is still insufficient for MCP.
        assert session.get(resource, timeout=30).status_code == 401
        response = session.get(discovery["authorization_endpoint"], params=auth_args, timeout=30)
        assert response.status_code == 200, response.status_code
        assert "Allow read-only reporting" in response.text
        csrf = html.fromstring(response.text).xpath('//input[@name="csrf_token"]/@value')[0]
        consent = dict(auth_args, decision="approve", csrf_token=csrf)
        response = session.post(discovery["authorization_endpoint"], data=consent, allow_redirects=False, timeout=30)
        assert response.status_code == 303, (response.status_code, response.text[:200])
        redirect = response.headers["Location"]
        assert urlsplit(redirect).netloc == "claude.ai"
        args = parse_qs(urlsplit(redirect).query)
        assert args["state"] == ["local-qa-state"]
        response = session.post(discovery["token_endpoint"], data={"grant_type": "authorization_code",
            "client_id": registered["client_id"], "code": args["code"][0], "code_verifier": verifier,
            "redirect_uri": registered["redirect_uris"][0], "resource": resource}, timeout=30)
        assert response.status_code == 200, response.status_code
        token = response.json()
        checks.append("Odoo sign-in, explicit consent, CSRF form, PKCE code exchange; cookies alone rejected")
        headers = {"Authorization": "Bearer " + token["access_token"], "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"}

        def rpc(method, params=None):
            payload = {"jsonrpc": "2.0", "id": 42, "method": method}
            if params is not None:
                payload["params"] = params
            response = session.post(resource, json=payload, headers=headers, timeout=30)
            assert response.status_code == 200, response.status_code
            return response.json()

        result = rpc("initialize", {"protocolVersion": "2025-11-25", "clientInfo": {"name": "local QA", "version": "1"}, "capabilities": {}})
        assert result["result"]["capabilities"] == {"tools": {}}
        assert len(rpc("tools/list")["result"]["tools"]) == 15
        response = session.post(resource, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers, timeout=30)
        assert response.status_code == 202
        info = rpc("tools/call", {"name": "connection_info"})["result"]["structuredContent"]
        assert info["user_id"] == user.id and info["read_only"] and info["cross_device_access"] and not info["superuser"]
        result = rpc("tools/call", {"name": "query", "arguments": {"spec": {"model": "res.partner",
            "domain": [["id", "in", [visible.id, hidden.id]]], "fields": ["id", "name"]}}})["result"]
        assert not result["isError"], result
        assert [r["id"] for r in result["structuredContent"]["rows"]] == visible.ids
        assert result["structuredContent"]["citation"]["url"].startswith(base + "/tradeline/mcp/source?")
        blocked = rpc("tools/call", {"name": "query", "arguments": {"spec": {"model": "res.partner", "fields": ["phone"]}}})
        assert blocked["result"]["isError"]
        assert rpc("tools/call", {"name": "write", "arguments": {}})["error"]["code"] == -32602
        checks.append("MCP handshake and 15 tools; real PostgreSQL query respects record/field permissions and citations")
        bad_origin = session.post(resource, json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers=dict(headers, Origin="https://evil.invalid"), timeout=30)
        assert bad_origin.status_code == 403
        refresh_args = {"grant_type": "refresh_token", "client_id": registered["client_id"], "refresh_token": token["refresh_token"], "resource": resource}
        response = session.post(discovery["token_endpoint"], data=refresh_args, timeout=30)
        assert response.status_code == 200
        rotated = response.json()
        assert session.get(resource, headers=headers, timeout=30).status_code == 401
        headers["Authorization"] = "Bearer " + rotated["access_token"]
        assert rpc("ping")["result"] == {}
        assert session.post(discovery["token_endpoint"], data=refresh_args, timeout=30).status_code == 400
        assert session.get(resource, headers=headers, timeout=30).status_code == 401
        checks.append("invalid Origin rejected; refresh rotation and replay revoke the complete token family")
    finally:
        env.cr.rollback()
        env.invalidate_all()
        settings.write(old_settings)
        policy.write(old_policy)
        icp.set_param("web.base.url", old_base)
        if client and client.exists():
            client.unlink()
        if rule and rule.exists():
            rule.unlink()
        if user and user.exists():
            user.unlink()
        for record in (visible, hidden, profile):
            if record and record.exists():
                record.unlink()
        env.cr.commit()
        session.close()
        if local_session is not None:
            http.root.session_store.delete(local_session)
    print("HOSTED_MCP_HTTP_CHECKS", len(checks), "PASS", checks)

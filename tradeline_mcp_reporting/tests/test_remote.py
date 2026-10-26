import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged
from ..oauth_utils import SCOPE, challenge, digest, opaque, registration, canonical_url
from ..protocol import CATALOG, handle_message


@tagged("post_install", "-at_install")
class TestHostedMCP(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": "Hosted MCP QA", "login": "hosted_mcp_qa",
            "groups_id": [(6, 0, cls.env.ref("tradeline_mcp_reporting.group_mcp_reporting_user").ids)]})
        cls.settings = cls.env["ai.copilot.settings"].sudo().get_singleton()
        cls.settings.write({"enable_mcp_reporting": True, "enable_mcp_remote": True})
        cls.grants = cls.env["tradeline.mcp.oauth.grant"]
        cls.resource = "https://tradelinestores.odoo.com/tradeline/mcp"
        cls.client = cls.env["tradeline.mcp.oauth.client"]._register_client({"client_name": "Claude",
            "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]})
        cls.verifier = "a" * 64
        cls.params = {"response_type": "code", "client_id": cls.client["client_id"], "scope": SCOPE,
            "redirect_uri": cls.client["redirect_uris"][0], "state": "test-state", "resource": cls.resource,
            "code_challenge": challenge(cls.verifier), "code_challenge_method": "S256"}

    def exchange(self, **changes):
        code = self.grants._issue_code(self.params, self.resource, self.user.id)
        params = {"grant_type": "authorization_code", "client_id": self.client["client_id"], "code": code,
            "code_verifier": self.verifier, "redirect_uri": self.params["redirect_uri"], "resource": self.resource}
        params.update(changes)
        return self.grants._exchange(params, self.resource)

    def test_authentication_and_only_hashes_stored(self):
        token = self.exchange()
        self.assertEqual(self.grants._authenticate(token["access_token"], self.resource), self.user.id)
        rows = self.grants.sudo().search([("user_id", "=", self.user.id)]).read(["code_hash", "access_hash", "refresh_hash"])
        self.assertNotIn(token["access_token"], json.dumps(rows))
        self.assertNotIn(token["refresh_token"], json.dumps(rows))
        self.assertIn(digest(token["access_token"]), json.dumps(rows))

    def test_code_is_single_use_and_untrusted_names_do_not_fill_registry(self):
        other = self.env["tradeline.mcp.oauth.client"]._register_client({"client_name": "Arbitrary label",
            "redirect_uris": self.client["redirect_uris"]})
        self.assertEqual(other["client_id"], self.client["client_id"])
        code = self.grants._issue_code(self.params, self.resource, self.user.id)
        exchange = {"grant_type": "authorization_code", "client_id": self.client["client_id"], "code": code,
            "code_verifier": self.verifier, "redirect_uri": self.params["redirect_uri"], "resource": self.resource}
        token = self.grants._exchange(exchange, self.resource)
        rejected = False
        try:
            self.grants._exchange(exchange, self.resource)
        except ValueError:
            rejected = True
        self.assertTrue(rejected)
        with self.assertRaises(AccessError):
            self.grants._authenticate(token["access_token"], self.resource)

    def test_resource_scope_pkce_redirect_and_expiry_fail_closed(self):
        for changes in [{"resource": self.resource + "/wrong"}, {"code_verifier": "b" * 64},
                        {"redirect_uri": "https://evil.invalid/"}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.exchange(**changes)
        token = self.exchange()
        with self.assertRaises(AccessError):
            self.grants._authenticate(token["access_token"], self.resource + "/wrong")
        grant = self.grants.sudo().search([("access_hash", "=", digest(token["access_token"]))])
        grant.expires_at = fields.Datetime.now() - timedelta(seconds=1)
        with self.assertRaises(AccessError):
            self.grants._authenticate(token["access_token"], self.resource)

    def test_refresh_rotates_and_replay_revokes_family(self):
        original = self.exchange()
        params = {"grant_type": "refresh_token", "client_id": self.client["client_id"],
            "refresh_token": original["refresh_token"], "resource": self.resource}
        rotated = self.grants._exchange(params, self.resource)
        self.assertEqual(self.grants._authenticate(rotated["access_token"], self.resource), self.user.id)
        with self.assertRaises(AccessError):
            self.grants._authenticate(original["access_token"], self.resource)
        # Odoo assertRaises rolls back its savepoint; the real controller catches
        # invalid_grant and commits replay revocation. Preserve that behavior.
        replay_rejected = False
        try:
            self.grants._exchange(params, self.resource)
        except ValueError:
            replay_rejected = True
        self.assertTrue(replay_rejected)
        with self.assertRaises(AccessError):
            self.grants._authenticate(rotated["access_token"], self.resource)

    def test_revocation_disabled_user_removed_group_and_switch(self):
        token = self.exchange()
        self.settings.enable_mcp_remote = False
        with self.assertRaises(AccessError):
            self.grants._authenticate(token["access_token"], self.resource)
        self.settings.enable_mcp_remote = True
        self.user.active = False
        with self.assertRaises(AccessError):
            self.grants._authenticate(token["access_token"], self.resource)
        self.user.active = True
        self.user.groups_id -= self.env.ref("tradeline_mcp_reporting.group_mcp_reporting_user")
        with self.assertRaises(AccessError):
            self.grants._authenticate(token["access_token"], self.resource)
        self.user.groups_id |= self.env.ref("tradeline_mcp_reporting.group_mcp_reporting_user")
        self.grants._revoke_token(token["refresh_token"], self.client["client_id"])
        with self.assertRaises(AccessError):
            self.grants._authenticate(token["access_token"], self.resource)

    def test_registration_and_auth_request_boundaries(self):
        for redirects in [["https://evil.invalid/callback"], ["http://claude.ai/api/mcp/auth_callback"],
                          ["https://claude.ai.evil.invalid/api/mcp/auth_callback"]]:
            with self.assertRaises(ValueError):
                registration({"redirect_uris": redirects})
        self.assertEqual(canonical_url("https://tradelinestores.odoo.com/"), "https://tradelinestores.odoo.com")
        with self.assertRaises(ValueError):
            canonical_url("http://tradelinestores.odoo.com")
        for changes in [{"scope": "write"}, {"code_challenge_method": "plain"}, {"state": ""}, {"resource": "https://evil.invalid"}]:
            params = dict(self.params, **changes)
            with self.assertRaises(ValueError):
                self.grants._authorize_request(params, self.resource)

    def test_oauth_models_never_discoverable_or_readable_by_reporter(self):
        service = self.env["ai.copilot.service"].with_user(self.user)
        for name in ["tradeline.mcp.oauth.client", "tradeline.mcp.oauth.grant"]:
            self.assertTrue(service._is_model_denied(name))
            with self.assertRaises(AccessError):
                self.env[name].with_user(self.user).search([])
        with self.assertRaises(AccessError):
            self.grants._issue_code(self.params, self.resource, 1)

    def test_protocol_catalogue_and_safe_errors(self):
        call = MagicMock(return_value={"citation": {"path": "/tradeline/mcp/source?tool=query"}})
        result, status = handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, call, "https://tradelinestores.odoo.com")
        self.assertEqual(status, 200)
        names = {t["name"] for t in result["result"]["tools"]}
        self.assertEqual(len(names), 15)
        self.assertNotIn("write", names)
        from ..models.bridge import TOOLS
        self.assertEqual(names, set(TOOLS))
        for item in CATALOG:
            self.assertTrue(item["annotations"]["readOnlyHint"])
        response, _ = handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "connection_info"}}, call, "https://tradelinestores.odoo.com")
        self.assertIn("https://tradelinestores.odoo.com/tradeline/mcp/source?", response["result"]["structuredContent"]["citation"]["url"])
        call.side_effect = ValueError("Unavailable; not zero.")
        response, _ = handle_message({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "query", "arguments": {"spec": {}}}}, call, "https://tradelinestores.odoo.com")
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}, call, "")[1], 202)
        self.assertEqual(handle_message([], call, "")[1], 400)

    def test_consent_and_mcp_controller_security(self):
        from ..controllers import remote
        fake = MagicMock()
        fake.env.uid = self.user.id
        fake.env.user.name = "<script>Bad name</script>"
        fake.env.user.company_ids.mapped.return_value = ["QA company"]
        fake.env.__getitem__.return_value._authorize_request.return_value.client_name = "<img src=x>"
        fake.httprequest.method = "GET"
        from werkzeug.datastructures import MultiDict
        fake.httprequest.args = MultiDict(self.params)
        fake.csrf_token.return_value = "safe-token"
        fake.make_response.side_effect = lambda body, **kwargs: remote.http.Response(body, **kwargs)
        with patch.object(remote, "request", fake), patch.object(remote, "base_url", return_value="https://tradelinestores.odoo.com"):
            response = remote.MCPRemote().authorize()
            body = response.get_data(as_text=True)
            self.assertNotIn("<script>", body)
            self.assertIn("&lt;script&gt;", body)
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
            self.assertIn("form-action 'self' https://claude.ai/api/mcp/auth_callback;", response.headers["Content-Security-Policy"])
            self.assertNotIn("*", response.headers["Content-Security-Policy"])
            self.assertIn('name="csrf_token"', body)
        self.assertTrue(remote.MCPRemote.authorize.original_routing["csrf"])
        self.assertTrue(remote.MCPRemote.connections.original_routing["csrf"])
        ordinary = dict(remote.consent_headers())["Content-Security-Policy"]
        self.assertIn("form-action 'self';", ordinary)
        self.assertNotIn("claude.ai", ordinary)

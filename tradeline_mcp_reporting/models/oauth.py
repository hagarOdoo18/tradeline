"""Private OAuth authentication state. Reporting tools cannot discover these models.

Only token digests are stored. All helpers are private (not RPC-callable). Sudo is
limited to this authentication state; business calls run as the named user.
"""
import json
import re
import secrets
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import AccessError
from ..oauth_utils import SCOPE, challenge, digest, opaque, registration


class MCPOAuthClient(models.Model):
    _name = "tradeline.mcp.oauth.client"
    _description = "Hosted reporting OAuth client"
    _rec_name = "client_name"

    client_id = fields.Char(required=True, index=True, copy=False)
    client_name = fields.Char(required=True)
    redirect_uris = fields.Json(required=True)
    metadata_hash = fields.Char(required=True, index=True)
    active = fields.Boolean(default=True)
    _sql_constraints = [("client_id_unique", "unique(client_id)", "Client IDs must be unique.")]

    @api.model
    def _register_client(self, metadata):
        clean = registration(metadata)
        # Only the verified Claude callback is accepted. Reuse one public client;
        # arbitrary anonymous client_name values must not fill registration rows.
        clean["client_name"] = "Claude"
        fingerprint = digest(json.dumps(clean, sort_keys=True))
        client = self.sudo().search([("metadata_hash", "=", fingerprint)], limit=1)
        if not client:
            if self.sudo().search_count([]) >= 200:
                raise ValueError("Client registration limit reached; contact the administrator.")
            client = self.sudo().create(dict(clean, client_id=opaque(), metadata_hash=fingerprint))
        return {"client_id": client.client_id, "client_name": client.client_name,
                "redirect_uris": client.redirect_uris, "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]}

    @api.model
    def _find(self, client_id):
        if not isinstance(client_id, str) or len(client_id) > 200:
            raise ValueError("Unknown OAuth client.")
        client = self.sudo().search([("client_id", "=", client_id)], limit=1)
        if not client:
            raise ValueError("Unknown OAuth client. Choose Register automatically in Claude.")
        return client


class MCPOAuthGrant(models.Model):
    _name = "tradeline.mcp.oauth.grant"
    _description = "Hosted reporting authorization"
    _rec_name = "client_id"

    client_id = fields.Many2one("tradeline.mcp.oauth.client", required=True, ondelete="cascade")
    user_id = fields.Many2one("res.users", required=True, ondelete="cascade", index=True)
    kind = fields.Selection([("code", "Authorization code"), ("token", "Access grant")], required=True)
    code_hash = fields.Char(index=True, copy=False)
    access_hash = fields.Char(index=True, copy=False)
    refresh_hash = fields.Char(index=True, copy=False)
    family = fields.Char(index=True, copy=False)
    resource = fields.Char(required=True)
    redirect_uri = fields.Char()
    code_challenge = fields.Char()
    expires_at = fields.Datetime(required=True)
    refresh_expires_at = fields.Datetime()
    used = fields.Boolean(default=False)
    revoked = fields.Boolean(default=False)
    _sql_constraints = [
        ("code_unique", "unique(code_hash)", "Authorization codes must be unique."),
        ("access_unique", "unique(access_hash)", "Access tokens must be unique."),
        ("refresh_unique", "unique(refresh_hash)", "Refresh tokens must be unique.")]

    @api.model
    def _check_enabled(self):
        settings = self.env["ai.copilot.settings"].sudo().search([], limit=1)
        if not settings or not settings.enable_mcp_reporting or not settings.enable_mcp_remote:
            raise AccessError("Hosted reporting is disabled by the administrator.")

    @api.model
    def _check_user(self, user_id):
        self._check_enabled()
        user = self.env["res.users"].sudo().browse(user_id).exists()
        if not user or user.id <= 1 or not user.active or user.share:
            raise AccessError("An approved named internal reporting user is required.")
        # Drop elevated authentication privileges before checking reporting access.
        self.env["tradeline.mcp.reporting"].with_user(user).sudo(False)._assert_mcp_access()
        return user

    @api.model
    def _authorize_request(self, params, resource):
        allowed = {"response_type", "client_id", "redirect_uri", "scope", "state", "code_challenge", "code_challenge_method", "resource"}
        if set(params) - allowed or params.get("response_type") != "code" or params.get("resource") != resource:
            raise ValueError("Invalid authorization request or resource.")
        client = self.env["tradeline.mcp.oauth.client"]._find(params.get("client_id"))
        if params.get("redirect_uri") not in client.redirect_uris or params.get("scope", SCOPE) != SCOPE:
            raise ValueError("Invalid redirect URI or reporting scope.")
        if params.get("code_challenge_method") != "S256" or not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.get("code_challenge", "")):
            raise ValueError("PKCE S256 is required.")
        if not isinstance(params.get("state"), str) or not 1 <= len(params["state"]) <= 1024:
            raise ValueError("OAuth state is required.")
        return client

    @api.model
    def _issue_code(self, params, resource, user_id):
        self._check_user(user_id)
        client = self._authorize_request(params, resource)
        # Bounded consent state, expiring after two minutes. No default grants.
        if self.sudo().search_count([("user_id", "=", user_id), ("kind", "=", "code"),
                ("expires_at", ">", fields.Datetime.now()), ("used", "=", False)]) >= 20:
            raise ValueError("Too many pending sign-ins; wait two minutes.")
        code = opaque()
        self.sudo().create({"client_id": client.id, "user_id": user_id, "kind": "code",
            "code_hash": digest(code), "family": opaque(), "resource": resource, "redirect_uri": params["redirect_uri"],
            "code_challenge": params["code_challenge"], "expires_at": fields.Datetime.now() + timedelta(minutes=2)})
        return code

    def _lock(self):
        self.ensure_one()
        # Fixed SQL locks only our OAuth metadata row, never a business table.
        self.env.cr.execute("SELECT id FROM tradeline_mcp_oauth_grant WHERE id = %s FOR UPDATE", [self.id])
        self.invalidate_recordset()

    @api.model
    def _new_tokens(self, grant, family=None):
        self._check_user(grant.user_id.id)
        access, refresh = opaque(), opaque()
        self.sudo().create({"client_id": grant.client_id.id, "user_id": grant.user_id.id, "kind": "token",
            "access_hash": digest(access), "refresh_hash": digest(refresh), "family": family or opaque(),
            "resource": grant.resource, "expires_at": fields.Datetime.now() + timedelta(hours=1),
            "refresh_expires_at": fields.Datetime.now() + timedelta(days=14)})
        return {"access_token": access, "refresh_token": refresh, "token_type": "Bearer", "expires_in": 3600, "scope": SCOPE}

    @api.model
    def _exchange(self, params, resource):
        self._check_enabled()
        if params.get("resource") != resource:
            raise ValueError("Invalid resource.")
        client = self.env["tradeline.mcp.oauth.client"]._find(params.get("client_id"))
        kind = params.get("grant_type")
        field, value = ("code_hash", params.get("code")) if kind == "authorization_code" else ("refresh_hash", params.get("refresh_token"))
        if kind not in {"authorization_code", "refresh_token"} or not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{64}", value):
            raise ValueError("Invalid grant.")
        grant = self.sudo().search([(field, "=", digest(value)), ("client_id", "=", client.id), ("resource", "=", resource)], limit=1)
        if not grant:
            raise ValueError("Invalid grant.")
        grant._lock()
        if grant.used or grant.revoked:
            if grant.family:
                self.sudo().search([("family", "=", grant.family)]).write({"revoked": True})
            raise ValueError("Invalid grant; reconnect in Claude.")
        if kind == "authorization_code":
            if grant.kind != "code" or grant.expires_at <= fields.Datetime.now() or params.get("redirect_uri") != grant.redirect_uri:
                raise ValueError("Invalid grant.")
            if not secrets.compare_digest(challenge(params.get("code_verifier")), grant.code_challenge):
                raise ValueError("Invalid grant.")
        elif grant.kind != "token" or grant.refresh_expires_at <= fields.Datetime.now():
            raise ValueError("Expired grant; reconnect in Claude.")
        result = self._new_tokens(grant, grant.family)
        grant.write({"used": True, "revoked": True})
        return result

    @api.model
    def _authenticate(self, token, resource):
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{64}", token):
            raise AccessError("Reporting authentication required.")
        grant = self.sudo().search([("kind", "=", "token"), ("access_hash", "=", digest(token)),
            ("resource", "=", resource), ("revoked", "=", False), ("used", "=", False),
            ("expires_at", ">", fields.Datetime.now()), ("client_id.active", "=", True)], limit=1)
        if not grant:
            raise AccessError("Reporting sign-in expired or was revoked.")
        return self._check_user(grant.user_id.id).id

    @api.model
    def _revoke_token(self, token, client_id):
        client = self.env["tradeline.mcp.oauth.client"]._find(client_id)
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{64}", token):
            return
        key = digest(token)
        grants = self.sudo().search([("client_id", "=", client.id), "|", ("access_hash", "=", key), ("refresh_hash", "=", key)])
        for grant in grants:
            self.sudo().search([("family", "=", grant.family)]).write({"revoked": True})

    @api.autovacuum
    def _cleanup_expired(self):
        cutoff = fields.Datetime.now() - timedelta(days=1)
        # Keep used refresh grants until expiry to detect rotation replay.
        self.sudo().search(["|", "&", ("kind", "=", "code"), ("expires_at", "<", cutoff),
            "&", ("kind", "=", "token"), ("refresh_expires_at", "<", cutoff)]).unlink()

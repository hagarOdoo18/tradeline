# -*- coding: utf-8 -*-
"""Bearer tokens for the confirmed order API.

The Shopify developer calls ``POST /api/shopify/v1/auth`` with the Store Name
and Client Secret of the Shopify instance and receives a token valid for
``TOKEN_LIFETIME``; the order endpoints accept that token in
``Authorization: Bearer <token>``.

Only a SHA-256 hash of the token is stored, so a database dump does not leak
usable tokens. The clear token is returned once, by the auth endpoint.
"""
import hashlib
import secrets
from datetime import timedelta

from odoo import api, fields, models

TOKEN_LIFETIME = timedelta(hours=24)


class ShopifyApiToken(models.Model):
    _name = 'shopify.api.token'
    _description = 'Shopify Order API Token'
    _order = 'create_date desc, id desc'

    instance_id = fields.Many2one('shopify.configuration',
                                  string='Shopify Instance', required=True,
                                  ondelete='cascade', index=True)
    token_hash = fields.Char(string='Token Hash', required=True, index=True,
                             copy=False, groups='base.group_system')
    expires_at = fields.Datetime(string='Expires At', required=True)
    last_used = fields.Datetime(string='Last Used', readonly=True)
    active = fields.Boolean(string='Active', default=True,
                            help='Untick to revoke the token immediately')
    is_valid = fields.Boolean(string='Valid', compute='_compute_is_valid')

    @api.depends('active', 'expires_at')
    def _compute_is_valid(self):
        now = fields.Datetime.now()
        for token in self:
            token.is_valid = bool(token.active and token.expires_at
                                  and token.expires_at > now)

    @staticmethod
    def _hash(token):
        return hashlib.sha256(token.encode('utf-8')).hexdigest()

    @api.model
    def _issue(self, instance):
        """Create a token for `instance`; returns
        ``(clear_token, record)``. The clear token is not stored."""
        token = secrets.token_urlsafe(32)
        record = self.sudo().create({
            'instance_id': instance.id,
            'token_hash': self._hash(token),
            'expires_at': fields.Datetime.now() + TOKEN_LIFETIME,
        })
        return token, record

    @api.model
    def _find(self, token):
        """Return the token record for a clear token (active or expired),
        or an empty recordset. Archived (revoked) tokens are not found."""
        if not token:
            return self.sudo().browse()
        return self.sudo().search(
            [('token_hash', '=', self._hash(token))], limit=1)

    def action_revoke(self):
        self.sudo().write({'active': False})
        return True

    @api.autovacuum
    def _gc_expired_tokens(self):
        """Delete tokens that expired more than a week ago."""
        limit = fields.Datetime.now() - timedelta(days=7)
        self.sudo().with_context(active_test=False).search(
            [('expires_at', '<', limit)]).unlink()

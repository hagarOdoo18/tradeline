# -*- coding: utf-8 -*-
from odoo import api, fields, models


class AiChatSession(models.Model):
    _name        = 'ai.chat.session'
    _description = 'AI Assistant Chat Session'
    _order       = 'write_date desc'

    name         = fields.Char(string='Session Name', default='New Chat', required=True)
    user_id      = fields.Many2one('res.users', string='User',
                                   default=lambda self: self.env.user, readonly=True)
    message_ids  = fields.One2many('ai.chat.message', 'session_id', string='Messages')
    message_count = fields.Integer(compute='_compute_message_count', string='Messages')
    active       = fields.Boolean(default=True)

    @api.depends('message_ids')
    def _compute_message_count(self):
        for rec in self:
            rec.message_count = len(rec.message_ids)

    def action_clear(self):
        """Delete all messages in this session."""
        self.message_ids.unlink()

    def get_history(self, limit=20):
        """Return last N messages as a list of dicts for AI context."""
        msgs = self.message_ids.search(
            [('session_id', '=', self.id)],
            order='create_date asc', limit=limit
        )
        return [{'role': m.role, 'content': m.content} for m in msgs]


class AiChatMessage(models.Model):
    _name        = 'ai.chat.message'
    _description = 'AI Chat Message'
    _order       = 'create_date asc'

    session_id  = fields.Many2one('ai.chat.session', string='Session',
                                  required=True, ondelete='cascade', index=True)
    role        = fields.Selection([
        ('user',      'User'),
        ('assistant', 'Assistant'),
        ('system',    'System'),
    ], string='Role', required=True, default='user')
    content     = fields.Text(string='Content', required=True)
    report_data = fields.Text(string='Structured Report Data (JSON)')

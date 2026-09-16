# -*- coding: utf-8 -*-
from odoo import models, fields, api


class PosSerialHistory(models.Model):
    """
    سجل تاريخ كل Serial — كل عملية بيع أو مرتجع تُسجَّل هنا.
    يساعد في تتبع دورة حياة الـ Serial كاملاً.
    """
    _name = 'pos.serial.history'
    _description = 'سجل تاريخ الرقم التسلسلي في POS'
    _order = 'date desc, id desc'
    _rec_name = 'lot_id'

    lot_id = fields.Many2one(
        comodel_name='stock.lot',
        string='الرقم التسلسلي',
        required=True,
        ondelete='cascade',
        index=True,
    )

    order_id = fields.Many2one(
        comodel_name='pos.order',
        string='أمر البيع',
        ondelete='set null',
    )

    order_line_id = fields.Many2one(
        comodel_name='pos.order.line',
        string='سطر الأمر',
        ondelete='set null',
    )

    operation = fields.Selection(
        selection=[
            ('sale',   '🛒 بيع'),
            ('return', '↩️ مرتجع'),
        ],
        string='نوع العملية',
        required=True,
    )

    date = fields.Datetime(
        string='التاريخ',
        default=fields.Datetime.now,
        required=True,
    )

    user_id = fields.Many2one(
        comodel_name='res.users',
        string='الموظف',
        default=lambda self: self.env.user,
    )

    note = fields.Char(string='ملاحظة')

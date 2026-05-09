# -*- coding: utf-8 -*-
from odoo import fields, models


class PaymentReportLine(models.TransientModel):
    _name        = 'payment.report'
    _description = 'Payment Report Line'
    _order       = 'date desc, id desc'

    wizard_id       = fields.Many2one('account.branch.report.wizard', ondelete='cascade')
    branch_name     = fields.Char(string='Branch',           readonly=True)
    date            = fields.Date(string='Date',             readonly=True)
    invoice_name    = fields.Char(string='Invoice',          readonly=True)
    sale_order_name = fields.Char(string='Sale Order',       readonly=True)
    partner_name    = fields.Char(string='Partner',          readonly=True)
    journal_name    = fields.Char(string='Journal / Method', readonly=True)
    source_type     = fields.Selection(
        selection=[
            ('invoice',       'Invoice'),
            ('invoice_pos',   'Invoice (POS)'),
            ('credit',        'Credit Note'),
            ('credit_pos',    'Credit Note (POS)'),
            ('order_payment', 'Order Payment'),
        ],
        string='Source',
        readonly=True,
    )
    amount = fields.Float(string='Amount', readonly=True, digits=(16, 2))

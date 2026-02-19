from odoo import models, fields


class AccountPayment(models.Model):
    _inherit = 'account.payment'

    reversed_original_payment_id = fields.Many2one(
        'account.payment',
        string='Original Payment',
        readonly=True,
        copy=False,
        help='The original inbound payment that was returned.',
    )

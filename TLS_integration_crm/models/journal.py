from odoo import fields, models, api



class AccountJournal(models.Model):
    """inherit account.journal to add fields and methods"""
    _inherit = 'account.journal'

    payment_type = fields.Selection(
        string=' Payment Type',
        selection=[('installment', 'Installment'),
                   ('cash', 'Cash'),
                   ('visa', 'Visa'),
                   ('pints', 'Pints'),
                   ('wallet', 'Wallet'),
                   ('Trade-In', 'Trade-In'),
                   ('credit', 'Credit'),
                   ('voucher', 'Voucher'),
                   ('withholding_tax','Withholding Tax')],
        required=False, )

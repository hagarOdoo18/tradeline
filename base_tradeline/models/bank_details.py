from odoo import fields, models, api


class BankDetails(models.Model):
    _name = 'bank.details'
    _description = 'Bank Details'

    name = fields.Char(required=True,string='Bank Name')
    account_name = fields.Char(
        string='Bank Account Name',
        required=True)

    account_number = fields.Char(
        string='Bank Account Number:',
        required=True)
    IBAN = fields.Char(
        string='IBAN',
        required=True)
    swift_code = fields.Char(
            string='Swift Code:',
            required=True)

    currency_id = fields.Many2one('res.currency',string='Account Currency',required=True,)

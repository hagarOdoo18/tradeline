# ?? 2015-2016 Akretion (http://www.akretion.com)
# @author Alexis de Lattre <alexis.delattre@akretion.com>
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo import models, fields


class AccountInvoice(models.Model):
    _name = 'account.invoice.tvc'


    invoice_number = fields.Text(
        string="Invoice Number",
        required=False)

    untaxed_amount = fields.Float(
        string='Untaxed amount',
        required=False)
    
    state = fields.Selection(
        string='State',
        selection=[('draft', 'Draft'),
                   ('error', 'Error'),
                   ('done', 'Done'), ],
        required=False, )
    customer_number = fields.Char(
        string='Customer Mobile',
        required=False)
    card = fields.Char(
        string='Card',
        required=False)
    note = fields.Text(
        string="Note",
        required=False)
    sent_date = fields.Date(
        string='Sent date',
        required=False)







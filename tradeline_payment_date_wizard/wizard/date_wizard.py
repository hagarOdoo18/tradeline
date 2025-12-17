from odoo import models, fields, api, _
from odoo.exceptions import ValidationError


class PaymentDateWizard(models.TransientModel):
    _name = 'payment.data.wizard'

    start_date = fields.Date(string="Start Date")
    end_date = fields.Date(string="End Date")

    @api.constrains('start_date', 'end_date')
    def _check_dates(self):
        if self.start_date > self.end_date:
            raise ValidationError(_('Start Date Can\'t be Before End Date'))

    def get_payment_range(self):
        return {
            'name': 'Payment In Range',
            'view_type': 'form',
            'view_mode': 'list,kanban,form,graph',
            'res_model': 'account.payment',
            'type': 'ir.actions.act_window',
            # 'view_id': self.env.ref('account.action_account_payments_payable').id,
            'domain': [('date', '>=', self.start_date), ('date', '<=', self.end_date)]
            }


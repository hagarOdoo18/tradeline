# Part of BrowseInfo. See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo import Command, models, fields, api, _

MAP_INVOICE_TYPE_PARTNER_TYPE = {
    'out_invoice': 'customer',
    'out_refund': 'customer',
    'in_invoice': 'supplier',
    'in_refund': 'supplier',
}

class AccountPaymentRegister(models.TransientModel):
    _inherit = 'account.payment.register'
    _name = 'account.payment.register'


    @api.model
    def _get_batch_journal(self, batch_result):
        """ Helper to compute the journal based on the batch.

        :param batch_result:    A batch computed by '_compute_batches'.
        :return:                An account.journal record.
        """
        payment_values = batch_result['payment_values']
        foreign_currency_id = payment_values['currency_id']
        partner_bank_id = payment_values['partner_bank_id']
        company = min(batch_result['lines'].company_id, key=lambda c: len(c.parent_ids))

        currency_domain = [('currency_id', '=', foreign_currency_id)]
        partner_bank_domain = [('bank_account_id', '=', partner_bank_id)]

        default_domain = [
            *self.env['account.journal']._check_company_domain(company),
            ('type', 'in', ('bank', 'cash', 'credit')),'|',('branch_id','=',False),('branch_id','in',self.env.user.branch_ids.ids),
            ('id', 'in', self.available_journal_ids.ids)
        ]
        if partner_bank_id:
            extra_domains = (
                currency_domain + partner_bank_domain,
                partner_bank_domain,
                currency_domain,
                [],
            )
        else:
            extra_domains = (
                currency_domain,
                [],
            )

        for extra_domain in extra_domains:
            journal = self.env['account.journal'].search(default_domain + extra_domain, limit=1)
            if journal:
                return journal

        return self.env['account.journal']

    @api.depends('available_journal_ids')
    def _compute_journal_id(self):
        for wizard in self:
            if wizard.journal_id in wizard.available_journal_ids:
                continue
            move_payment_method_lines = wizard.line_ids.move_id.preferred_payment_method_line_id
            if move_payment_method_lines and len(move_payment_method_lines) == 1:
                wizard.journal_id = move_payment_method_lines.journal_id
            elif wizard.can_edit_wizard:
                batch = wizard.batches[0]
                wizard.journal_id = wizard._get_batch_journal(batch)
            else:
                wizard.journal_id = self.env['account.journal'].search([
                    *self.env['account.journal']._check_company_domain(wizard.company_id),
                    ('type', 'in', ('bank', 'cash', 'credit')),'|',('branch_id','=',False),('branch_id','in',self.env.user.branch_ids.ids),

                ], limit=1)

    @api.depends('payment_type', 'company_id', 'can_edit_wizard')
    def _compute_available_journal_ids(self):
        for wizard in self:
            available_journals = self.env['account.journal']
            for batch in wizard.batches:
                available_journals |= wizard._get_batch_available_journals(batch)
            wizard.available_journal_ids =self.env['account.journal'].search([
                    *self.env['account.journal']._check_company_domain(wizard.company_id),
                    ('type', 'in', ('bank', 'cash', 'credit')),'|',('branch_id','=',False),('branch_id','in',self.env.user.branch_ids.ids),

                ],)

    def _create_payments(self):

        payments = super(AccountPaymentRegister, self)._create_payments()
        for payment in payments:
            payment.action_validate()
        return payments

class AccountPayment(models.Model):
    _inherit = 'account.payment'

    def compute_branches(self, fields):
        for rec in self:
            invoice_defaults = rec.reconciled_invoice_ids
            if invoice_defaults and len(invoice_defaults) == 1:
                invoice = invoice_defaults[0]
                rec.branch_id = invoice.branch_id.id
            else:
                rec.branch_id=False

    branch_id = fields.Many2one('res.branch',readonly=True,compute="compute_branches")

    # @api.onchange('branch_id')
    # def _onchange_branch_id(self):
    #     selected_brach = self.branch_id
    #     if selected_brach:
    #         user_id = self.env['res.users'].browse(self.env.uid)
    #         user_branch = user_id.sudo().branch_id
    #         if user_branch and user_branch.id != selected_brach.id:
    #             raise UserError("Please select active branch only. Other may create the Multi branch issue. \n\ne.g: If you wish to add other branch then Switch branch from the header and set that.")

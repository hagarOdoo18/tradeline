from odoo import fields, models, api

class PosConfig(models.Model):
    _inherit = 'pos.config'



    def _default_invoice_journal(self):

        return self.env['account.journal'].search([
            *self.env['account.journal']._check_company_domain(self.env.company),
            ('type', '=', 'sale'),('currency_id','=',74)
        ], limit=1)

    invoice_journal_id = fields.Many2one(
        'account.journal', string='Invoice Journal',
        check_company=True,
        domain=[('type', '=', 'sale')],
        help="Accounting journal used to create invoices.",
        default=_default_invoice_journal)



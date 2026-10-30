from odoo import models


class AccountEdiFormat(models.Model):
    _inherit = 'account.edi.format'

    def _check_move_configuration(self, invoice):
        company = invoice.company_id.sudo()
        if (self.code == 'eg_eta' and invoice.move_type in ('out_invoice', 'out_refund')
                and company.client_id and company.client_secret):
            # Native thumb-drive settings do not apply to the configured external signer.
            return []
        return super()._check_move_configuration(invoice)

    def _l10n_eg_edi_post_invoice(self, invoice):
        company = invoice.company_id.sudo()
        if invoice.move_type in ('out_invoice', 'out_refund'):
            # One submission owner. The standard pipeline must not send a second copy.
            return {invoice: invoice._eta_native_result()}
        return super()._l10n_eg_edi_post_invoice(invoice)

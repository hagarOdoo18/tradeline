from odoo import models, _
from odoo.exceptions import UserError


class AccountEdiFormat(models.Model):
    _inherit = 'account.edi.format'

    def _l10n_eg_eta_connect_to_server(self, request_data, request_url, method,
            is_access_token_req=False, production_enviroment=False):
        # Retain native receipt fields for history, but never run Odoo's ETA
        # transport. Signing, submission, status and cancellation use Ahmed's
        # integration and its configured company credentials only.
        return {'error': _('The Odoo ETA connector is disabled. Use Ahmed Salama ETA via Sync / ETA Status.'),
            'blocking_level': 'error'}

    def _check_move_configuration(self, invoice):
        company = invoice.company_id.sudo()
        if self.code == 'eg_eta' and invoice.move_type in ('out_invoice', 'out_refund'):
            # Never direct staff to native thumb-drive configuration.
            return [] if company.client_id and company.client_secret else [
                _('Configure Ahmed Salama ETA credentials on this invoice company. The Odoo ETA sender is disabled.')]
        return super()._check_move_configuration(invoice)

    def _l10n_eg_edi_post_invoice(self, invoice):
        company = invoice.company_id.sudo()
        if invoice.move_type in ('out_invoice', 'out_refund'):
            # One submission owner. The standard pipeline must not send a second copy.
            return {invoice: invoice._eta_native_result()}
        return super()._l10n_eg_edi_post_invoice(invoice)


class EtaNativeSigningDisabled(models.Model):
    _inherit = 'l10n_eg_edi.thumb.drive'

    def action_sign_invoices(self, invoice_ids):
        raise UserError(_('Odoo native ETA signing is disabled. Use Ahmed Salama ETA via Sync.'))

    def set_signature_data(self, invoices):
        # An old browser callback must not replace a preserved authority archive.
        raise UserError(_('Odoo native ETA signing is disabled. Use Ahmed Salama ETA via Sync.'))

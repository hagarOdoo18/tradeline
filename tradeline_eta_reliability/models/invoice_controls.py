"""One staff interface for ETA; retain native receipt history and other EDIs."""
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class EtaInvoiceControls(models.Model):
    _inherit = 'account.move'

    eta_native_edi_only = fields.Boolean(compute='_compute_eta_native_controls', compute_sudo=True)
    eta_native_pending_only = fields.Boolean(compute='_compute_eta_native_controls', compute_sudo=True)
    eta_native_error_only = fields.Boolean(compute='_compute_eta_native_controls', compute_sudo=True)

    @api.depends('move_type', 'edi_document_ids', 'edi_document_ids.state',
                 'edi_document_ids.error', 'edi_document_ids.blocking_level',
                 'edi_document_ids.edi_format_id', 'edi_document_ids.edi_format_id.code')
    def _compute_eta_native_controls(self):
        for move in self:
            owned = move.move_type in ('out_invoice', 'out_refund')
            eta = move.edi_document_ids.filtered(lambda doc: doc.edi_format_id.code == 'eg_eta')
            others = move.edi_document_ids - eta
            pending = lambda doc: doc.state in ('to_send', 'to_cancel') and doc.blocking_level != 'error' and doc.edi_format_id._needs_web_services()
            move.eta_native_edi_only = bool(owned and eta and not others)
            move.eta_native_pending_only = bool(owned and eta.filtered(pending) and not others.filtered(pending))
            move.eta_native_error_only = bool(owned and eta.filtered('error') and not others.filtered('error'))

    def button_process_edi_web_services(self):
        self.ensure_one()
        # Old open browsers can retain the native button. Use the same durable
        # staff operation rather than clearing native errors or running another
        # EDI dispatcher. The background queue and native receipt records stay.
        if self.eta_native_edi_only:
            if self.edi_document_ids.filtered(lambda doc: doc.state == 'to_cancel'):
                raise UserError(_('This invoice has an EDI cancellation request. Central accounting must reconcile it before sending or correcting the invoice.'))
            if (self.e_invoice_uuid or self.l10n_eg_uuid or self.e_invoice_sent
                    or self.eta_staff_state in ('valid', 'accepted', 'unknown', 'delivery_review', 'review')):
                return self.action_eta_check_status_now()
            return self.action_eta_send_now()
        return super().button_process_edi_web_services()

    def action_retry_edi_documents_error(self):
        if len(self) == 1 and self.eta_native_edi_only:
            return self.button_process_edi_web_services()
        return super().action_retry_edi_documents_error()

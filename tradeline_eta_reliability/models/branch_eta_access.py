"""ETA staff controls are central-only; ordinary invoice operations stay intact."""
from odoo import api, fields, models, _
from odoo.exceptions import UserError, AccessError
from odoo.osv import expression


class EtaBranchInvoiceAccess(models.Model):
    _inherit = 'account.move'

    eta_branch_hide_native_controls = fields.Boolean(compute='_compute_eta_branch_controls')
    eta_user_is_admin = fields.Boolean(compute='_compute_eta_user_is_admin',
        help='Technical: current user is a system administrator (controls ETA send visibility).')

    @api.depends_context('uid')
    def _compute_eta_user_is_admin(self):
        is_admin = self.env.user.has_group('base.group_system')
        for move in self:
            move.eta_user_is_admin = is_admin

    @api.depends_context('uid')
    @api.depends('move_type', 'edi_document_ids', 'edi_document_ids.edi_format_id.code')
    def _compute_eta_branch_controls(self):
        central = self.env['res.users']._eta_has_workspace_access()
        for move in self:
            move.eta_branch_hide_native_controls = bool(not central and
                move.move_type in ('out_invoice', 'out_refund') and
                move.edi_document_ids.filtered(lambda doc: doc.edi_format_id.code == 'eg_eta'))

    def _eta_require_central_staff(self):
        self.env['res.users']._eta_check_workspace_access()

    def _eta_require_admin(self):
        # Sending/resending to ETA is restricted to administrators; other central
        # staff keep read/status access and automatic queue submission is unaffected.
        self._eta_require_central_staff()
        if not self.env.user.has_group('base.group_system'):
            raise AccessError(_('Only an administrator can send or resend documents to ETA. '
                                'You can still check the ETA status.'))

    def action_eta_send_now(self):
        self._eta_require_admin()
        return super().action_eta_send_now()

    def action_eta_check_status_now(self):
        self._eta_require_central_staff()
        return super().action_eta_check_status_now()

    def action_eta_execute_staff_request(self, request_id):
        self._eta_require_central_staff()
        return super().action_eta_execute_staff_request(request_id)

    def action_send_electronic_invoice(self):
        self._eta_require_admin()
        return super().action_send_electronic_invoice()

    def action_update_electronic_invoice_status(self):
        self._eta_require_central_staff()
        return super().action_update_electronic_invoice_status()

    def action_cancel_electronic_invoice(self):
        self._eta_require_central_staff()
        return super().action_cancel_electronic_invoice()

    def action_update_electronic_invoice_pdf(self):
        self._eta_require_central_staff()
        return super().action_update_electronic_invoice_pdf()

    def action_generate_eta_json(self):
        self._eta_require_central_staff()
        return super().action_generate_eta_json()

    def action_post_sign_invoices(self):
        self._eta_require_central_staff()
        return super().action_post_sign_invoices()

    def action_get_eta_invoice_pdf(self):
        self._eta_require_central_staff()
        return super().action_get_eta_invoice_pdf()

    def action_sign_invoice(self):
        self._eta_require_central_staff()
        return super().action_sign_invoice()

    def _eta_other_native_documents(self):
        return self.edi_document_ids.filtered(lambda doc: doc.edi_format_id.code != 'eg_eta')

    def button_process_edi_web_services(self):
        if not self.env['res.users']._eta_has_workspace_access() and self.edi_document_ids.filtered(
                lambda doc: doc.edi_format_id.code == 'eg_eta'):
            others = self._eta_other_native_documents().filtered(
                lambda doc: doc.state in ('to_send', 'to_cancel') and doc.blocking_level != 'error')
            if not others:
                self._eta_require_central_staff()
            return others._process_documents_web_services(with_commit=False)
        return super().button_process_edi_web_services()

    def action_retry_edi_documents_error(self):
        if not self.env['res.users']._eta_has_workspace_access() and self.edi_document_ids.filtered(
                lambda doc: doc.edi_format_id.code == 'eg_eta'):
            others = self._eta_other_native_documents()
            if not others:
                self._eta_require_central_staff()
            others.write({'error': False, 'blocking_level': False})
            return others._process_documents_web_services(with_commit=False)
        return super().action_retry_edi_documents_error()

    def action_process_edi_web_services(self, with_commit=True):
        if not self.env['res.users']._eta_has_workspace_access() and self.edi_document_ids.filtered(
                lambda doc: doc.edi_format_id.code == 'eg_eta'):
            others = self._eta_other_native_documents().filtered(
                lambda doc: doc.state in ('to_send', 'to_cancel') and doc.blocking_level != 'error')
            if not others:
                self._eta_require_central_staff()
            return others._process_documents_web_services(with_commit=with_commit)
        return super().action_process_edi_web_services(with_commit=with_commit)

    def button_cancel_posted_moves(self):
        if self.edi_document_ids.filtered(lambda doc: doc.edi_format_id.code == 'eg_eta'):
            self._eta_require_central_staff()
        return super().button_cancel_posted_moves()

    def button_abandon_cancel_posted_posted_moves(self):
        if self.edi_document_ids.filtered(lambda doc: doc.edi_format_id.code == 'eg_eta'):
            self._eta_require_central_staff()
        return super().button_abandon_cancel_posted_posted_moves()

    def button_force_cancel(self):
        if self.edi_document_ids.filtered(lambda doc: doc.edi_format_id.code == 'eg_eta'):
            self._eta_require_central_staff()
        return super().button_force_cancel()

    @api.depends_context('uid')
    @api.depends('edi_document_ids', 'edi_document_ids.state', 'edi_document_ids.blocking_level',
                 'edi_document_ids.edi_format_id', 'edi_document_ids.edi_format_id.name')
    def _compute_edi_web_services_to_process(self):
        super()._compute_edi_web_services_to_process()
        if not self.env['res.users']._eta_has_workspace_access():
            for move in self:
                docs = move._eta_other_native_documents().filtered(
                    lambda doc: doc.state in ('to_send', 'to_cancel') and doc.blocking_level != 'error')
                move.edi_web_services_to_process = ', '.join(
                    docs.edi_format_id.filtered(lambda fmt: fmt._needs_web_services()).mapped('name'))

    @api.depends_context('uid')
    @api.depends('edi_document_ids.error')
    def _compute_edi_error_count(self):
        super()._compute_edi_error_count()
        if not self.env['res.users']._eta_has_workspace_access():
            for move in self:
                move.edi_error_count = len(move._eta_other_native_documents().filtered('error'))

    @api.depends_context('uid')
    @api.depends('edi_error_count', 'edi_document_ids.error', 'edi_document_ids.blocking_level')
    def _compute_edi_error_message(self):
        # The base method assumes its error count covers every native document.
        # Format other-EDI notices independently for a branch so ETA-only notices
        # disappear without suppressing another integration's real errors.
        if self.env['res.users']._eta_has_workspace_access():
            return super()._compute_edi_error_message()
        for move in self:
            errors = move._eta_other_native_documents().filtered('error')
            if not errors:
                move.edi_error_message = move.edi_blocking_level = False
            elif len(errors) == 1:
                move.edi_error_message = errors.error
                move.edi_blocking_level = errors.blocking_level
            else:
                levels = set(errors.mapped('blocking_level'))
                level = 'error' if 'error' in levels else 'warning' if 'warning' in levels else 'info'
                move.edi_blocking_level = level
                move.edi_error_message = _('%s electronic invoicing message(s)', len(errors))

    def _track_filter_for_display(self, tracking_values):
        values = super()._track_filter_for_display(tracking_values)
        if self.env['res.users']._eta_has_workspace_access():
            return values
        return values.filtered(lambda value: not value.field_id or not (
            value.field_id.model == 'account.move' and value.field_id.name.startswith(
                ('e_invoice_', 'eta_', 'l10n_eg_'))))

    def _track_get_fields(self):
        # ETA has its own private request, receipt and job audit. Do not copy
        # future technical status/UUID updates into the public invoice chatter
        # or its live broadcasts; normal invoice/payment tracking is retained.
        return {name for name in super()._track_get_fields()
                if not name.startswith(('e_invoice_', 'eta_', 'l10n_eg_'))}

    def button_draft(self):
        if not self.env['res.users']._eta_has_workspace_access():
            controlled = self.filtered(lambda move: move._eta_controls_draft_reset())
            if any(not move._eta_can_reset_unsent() for move in controlled):
                raise UserError(_('Central accounting must review changes to this posted invoice.'))
        return super().button_draft()


class EtaBranchChatterDisplay(models.Model):
    _inherit = 'mail.message'

    @api.model
    def _message_fetch(self, domain, search_term=None, before=None, after=None, around=None, limit=30):
        if not self.env['res.users']._eta_has_workspace_access():
            prefixes = ('<p>ETA direct send requested by ', '<p>ETA status check requested by ',
                        'ETA direct send requested by ', 'ETA status check requested by ')
            generated_note = expression.AND([
                [('model', '=', 'account.move'), ('subtype_id', '=', self.env.ref('mail.mt_note').id),
                 ('message_type', '=', 'notification'), ('tracking_value_ids', '=', False),
                 ('attachment_ids', '=', False)],
                expression.OR([[('body', '=like', prefix + '%')] for prefix in prefixes])])
            # Display-only filtering of this module's exact operation-note
            # framing. Keep financial tracking, mixed notes and attachments;
            # historical messages and raw model permissions are unchanged.
            domain = expression.AND([domain, ['!'] + expression.normalize_domain(generated_note)])
        return super()._message_fetch(domain, search_term=search_term, before=before,
                                      after=after, around=around, limit=limit)

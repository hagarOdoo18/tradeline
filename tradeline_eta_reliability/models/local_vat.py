"""Protected local VAT reclassification; never reset, post, or send an ETA invoice."""
import hashlib
import json

import psycopg2

from odoo import api, fields, models, Command, _
from odoo.exceptions import AccessError, UserError


_LOCAL_VAT_EDIT = object()  # An RPC context cannot manufacture this capability.
_LEGACY_CREDIT_ROUTE = object()  # Retained only for trusted internal history tools/tests.
_LOCAL_FIELDS = {
    'eta_local_vat_applied_at', 'eta_local_vat_applied_by',
    'eta_local_vat_audit_id', 'eta_local_vat_audit_file', 'eta_local_vat_receipt_uuid',
    'eta_local_vat_net_amount', 'eta_local_vat_tax_amount', 'eta_local_vat_gross_amount',
}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


class EtaLocalVatMove(models.Model):
    _inherit = 'account.move'

    eta_local_vat_applied_at = fields.Datetime(string='Odoo VAT corrected on', readonly=True, copy=False)
    eta_local_vat_applied_by = fields.Many2one('res.users', string='Odoo VAT corrected by', readonly=True, copy=False)
    eta_local_vat_audit_id = fields.Many2one('ir.attachment', string='Local VAT amendment audit', readonly=True, copy=False,
        groups='tradeline_eta_reliability.group_eta_workspace')
    eta_local_vat_audit_file = fields.Binary(string='Private local VAT amendment evidence', attachment=True,
        readonly=True, copy=False, groups='tradeline_eta_reliability.group_eta_workspace')
    eta_local_vat_receipt_uuid = fields.Char(string='Preserved ETA receipt for local VAT correction', readonly=True, copy=False,
        groups='tradeline_eta_reliability.group_eta_workspace')
    eta_local_vat_net_amount = fields.Monetary(string='Verified local net amount', readonly=True, copy=False,
        groups='tradeline_eta_reliability.group_eta_workspace')
    eta_local_vat_tax_amount = fields.Monetary(string='Verified local tax amount', readonly=True, copy=False,
        groups='tradeline_eta_reliability.group_eta_workspace')
    eta_local_vat_gross_amount = fields.Monetary(string='Verified unchanged local gross', readonly=True, copy=False,
        groups='tradeline_eta_reliability.group_eta_workspace')

    @api.model_create_multi
    def create(self, vals_list):
        if self.env.context.get('_eta_local_vat_edit') is not _LOCAL_VAT_EDIT and any(_LOCAL_FIELDS & set(vals) for vals in vals_list):
            raise AccessError(_('Local VAT audit fields can only be recorded by the verified amendment.'))
        return super().create(vals_list)

    def write(self, vals):
        if _LOCAL_FIELDS & set(vals) and self.env.context.get('_eta_local_vat_edit') is not _LOCAL_VAT_EDIT:
            raise AccessError(_('Local VAT audit fields can only be recorded by the verified amendment.'))
        return super().write(vals)

    def _eta_local_vat_has_history(self):
        self.ensure_one()
        job = self.eta_job_ids.sudo()
        if (self.e_invoice_uuid or self.l10n_eg_uuid or self.e_invoice_sent
                or self.eta_local_vat_applied_at or self.sudo().eta_local_vat_receipt_uuid
                or self.e_invoice_json or self.invoice_signed or self.l10n_eg_submission_number
                or self.l10n_eg_eta_json_doc_id or self.e_invoice_status in ('Submitted', 'Valid', 'Invalid', 'Rejected', 'Cancelled')
                or job and (job.attempts or job.attempted_at or job.payload or job.payload_hash
                    or job.submission_uuid or job.duplicate_retry_at or job.duplicate_payload_hash
                    or job.historical_review or job.state in ('unknown', 'accepted', 'valid', 'invalid', 'review'))
                or self.edi_document_ids.filtered(lambda doc: doc.edi_format_id.code == 'eg_eta'
                    and (doc.state in ('sent', 'to_cancel', 'cancelled') or doc.attachment_id))):
            return True
        return bool(self.env['ir.attachment'].sudo().search_count([
            ('res_model', '=', 'account.move'), ('res_id', '=', self.id),
            ('name', '=like', 'ETA_%.json'), ('mimetype', '=', 'application/json')], limit=1))

    def _eta_apply14_requires_review(self):
        self.ensure_one()
        return self._eta_local_vat_has_history() and not self._eta_can_reset_unsent()

    def action_apply_14_tax(self):
        # Keep Mosad's implementation byte-for-byte. It remains the path only
        # for invoices with no authority history to preserve.
        if self.filtered(lambda move: move._eta_apply14_requires_review()):
            return self.action_eta_apply_local_vat()
        return super().action_apply_14_tax()

    def action_eta_correct_vat(self):
        self.ensure_one()
        history = self._eta_local_vat_has_history()
        if self.e_invoice_status == 'Valid' and history:
            return self.action_eta_local_vat_preview()
        if history and not self._eta_can_reset_unsent() and self.e_invoice_status not in ('Invalid', 'Rejected'):
            raise UserError(_('The earlier ETA submission is not finally approved or confirmed invalid. Check its status before correcting VAT. No invoice was reset or resent.'))
        action = super().action_eta_correct_vat()
        tax = self._eta_local_vat_tax()
        if tax:
            action['context'] = dict(action.get('context', {}), default_tax_id=tax.id)
        return action

    def _eta_local_vat_is_current(self, vat_missing=None):
        """UI hook: local accounting corrected, without rewriting receipt facts."""
        self.ensure_one()
        move = self.sudo()
        if vat_missing is None:
            vat_missing = move.eta_vat_missing
        uuid = move.e_invoice_uuid or move.l10n_eg_uuid
        audit = move.eta_local_vat_audit_id
        job = move.eta_job_ids.sudo()
        if (not move.eta_local_vat_applied_at or not move.eta_local_vat_applied_by
                or move.state != 'posted' or move.e_invoice_status != 'Valid'
                or not uuid or move.eta_local_vat_receipt_uuid != uuid
                or move.e_invoice_uuid and move.l10n_eg_uuid and move.e_invoice_uuid != move.l10n_eg_uuid
                or job and (job.state != 'valid' or job.duplicate_retry_at or job.duplicate_payload_hash or job.historical_review)
                or move.eta_submission_pending or move.eta_vat_original_id
                or move.eta_vat_correction_ids.filtered(lambda item: item.state != 'cancel')
                or move.reversal_move_ids.filtered(lambda item: item.state != 'cancel')
                or move.edi_document_ids.filtered(lambda item: item.edi_format_id.code != 'eg_eta'
                    and item.state in ('to_send', 'to_cancel'))
                or not audit or audit.res_model != 'account.move' or audit.res_id != move.id
                or audit.company_id != move.company_id or audit.public
                or audit.res_field != 'eta_local_vat_audit_file' or vat_missing
                or any(move.currency_id.compare_amounts(stored, actual) for stored, actual in [
                    (move.eta_local_vat_net_amount, move.amount_untaxed),
                    (move.eta_local_vat_tax_amount, move.amount_tax),
                    (move.eta_local_vat_gross_amount, move.amount_total)])):
            return False
        try:
            proof = json.loads(audit.raw)
            return bool(proof.get('kind') == 'local_accounting_vat14_only'
                and proof.get('move_id') == move.id and proof.get('preserved_eta_uuid') == uuid
                and proof.get('user_id') == move.eta_local_vat_applied_by.id
                and all(not move.currency_id.compare_amounts(proof.get('after', {}).get(key, -1), amount)
                    for key, amount in [('untaxed', move.amount_untaxed), ('tax', move.amount_tax), ('total', move.amount_total)]))
        except (TypeError, ValueError, AttributeError):
            return False

    def _eta_local_vat_check_access(self):
        self.env['res.users']._eta_check_workspace_access()
        self.check_access('write')
        if not self.env.user.has_group('account.group_account_invoice'):
            raise AccessError(_('Invoice accounting permissions are required to apply local VAT.'))

    def _eta_local_vat_validate(self, tax):
        self.ensure_one()
        self._eta_local_vat_check_access()
        if self.move_type != 'out_invoice' or self.state != 'posted':
            raise UserError(_('Local VAT correction is available for a posted customer invoice.'))
        # Native Egyptian to_send/to_cancel markers are not authority outcomes.
        # An Odoo-only amendment preserves them exactly; it never processes EDI.
        uuid = self.e_invoice_uuid or self.l10n_eg_uuid
        job = self.eta_job_ids.sudo()
        if (not uuid or self.e_invoice_status != 'Valid'
                or self.e_invoice_uuid and self.l10n_eg_uuid and self.e_invoice_uuid != self.l10n_eg_uuid
                or job and job.state != 'valid'
                or job and (job.duplicate_retry_at or job.duplicate_payload_hash or job.historical_review)
                or self.eta_submission_pending
                or self.edi_document_ids.filtered(lambda doc: doc.edi_format_id.code != 'eg_eta'
                    and doc.state in ('to_send', 'to_cancel'))):
            raise UserError(_('The earlier ETA submission must be finally approved with no pending send, retry, or receipt conflict before a local-only amendment. No ETA operation was started.'))
        if self.eta_vat_original_id or self.eta_vat_correction_ids.filtered(lambda move: move.state != 'cancel') \
                or self.reversal_move_ids.filtered(lambda move: move.state != 'cancel'):
            raise UserError(_('This invoice already has a credit or correction document. Central accounting must review that existing correction first.'))
        if (self.inalterable_hash or self.secure_sequence_number or self.journal_id.restrict_mode_hash_table
                or self.company_id.check_account_audit_trail):
            raise UserError(_('This posted entry is protected by an accounting hash or audit trail and cannot be amended in place.'))
        self._check_fiscal_lock_dates()
        violations = self.company_id._get_lock_date_violations(self.date,
            fiscalyear=True, sale=True, purchase=False, tax=True, hard=True)
        if violations:
            raise UserError(_('The invoice date is in a closed accounting or tax period. No local amendment was made.'))
        self.line_ids._check_tax_lock_date()
        if self.tax_cash_basis_rec_id or self.tax_cash_basis_origin_move_id \
                or self.line_ids.analytic_line_ids or any(self.invoice_line_ids.mapped('analytic_distribution')):
            raise UserError(_('Invoices with cash-basis tax entries or analytic allocations need a separate accounting review before a local VAT amendment.'))
        if (not tax or not tax.active or tax.company_id != self.company_id or tax.type_tax_use != 'sale'
                or tax.amount_type != 'percent' or abs(tax.amount-14) > .000001
                or tax not in self._eta_vat_taxes() or tax.tax_exigibility != 'on_invoice'):
            raise UserError(_('Select the active 14% invoice-basis VAT sales tax from this invoice company.'))

    def _eta_local_vat_tax(self):
        self.ensure_one()
        choices = self.env['account.tax'].search([
            ('company_id', '=', self.company_id.id), ('active', '=', True),
            ('type_tax_use', '=', 'sale'), ('amount_type', '=', 'percent'), ('amount', '=', 14)]) & self._eta_vat_taxes()
        preferred = self.company_id.account_sale_tax_id & choices
        return preferred[:1] or choices.filtered('price_include')[:1] or choices[:1]

    def _eta_local_vat_plan(self, tax):
        self.ensure_one()
        commands, untaxed, total = [], 0.0, 0.0
        vat_taxes = self._eta_vat_taxes()
        for line in self.invoice_line_ids.filtered(lambda line: line.display_type == 'product'):
            eligible = bool(line.product_id and line.product_id.type != 'service')
            taxes = ((line.tax_ids - vat_taxes) | tax) if eligible else line.tax_ids
            values = taxes.compute_all(line.price_unit * (1-line.discount/100), currency=self.currency_id,
                quantity=line.quantity, product=line.product_id, partner=self.partner_id, is_refund=False)
            untaxed += values['total_excluded']
            total += values['total_included']
            if eligible and taxes != line.tax_ids:
                commands.append(Command.update(line.id, {'tax_ids': [Command.set(taxes.ids)]}))
        return {'commands': commands, 'untaxed': self.currency_id.round(untaxed),
            'tax': self.currency_id.round(total-untaxed), 'total': self.currency_id.round(total)}

    def _eta_local_vat_transport_snapshot(self):
        self.ensure_one()
        move_fields = [name for name in self._fields if name.startswith(('e_invoice_', 'l10n_eg_'))]
        move_fields += [name for name in ('invoice_signed', 'use_static_signature', 'static_signature',
            'static_sign_url', 'pdf_name', 'eta_submission_pending', 'eta_job_ids',
            'eta_vat_original_id', 'eta_vat_correction_ids', 'eta_receipt_vat_uuid',
            'eta_receipt_vat_checked_at', 'eta_receipt_vat_missing', 'eta_receipt_vat_egp') if name in self._fields]
        jobs = self.eta_job_ids.sudo()
        job_fields = [name for name, field in jobs._fields.items() if field.store and name != 'display_name']
        native = self.edi_document_ids.sudo()
        native_fields = [name for name, field in native._fields.items() if field.store and name != 'display_name']
        attachments = self.env['ir.attachment'].sudo().search([
            ('res_model', '=', 'account.move'), ('res_id', '=', self.id),
            ('id', '!=', self.eta_local_vat_audit_id.id or 0)], order='id')
        return {'move': self.sudo().read(move_fields)[0], 'jobs': jobs.read(job_fields),
            'native': native.read(native_fields), 'attachments': attachments.read([
                'name', 'res_model', 'res_id', 'res_field', 'company_id', 'mimetype', 'checksum', 'public', 'write_date'])}

    def _eta_local_vat_financial_snapshot(self):
        self.ensure_one()
        terms = self.line_ids.filtered(lambda line: line.account_id.account_type in ('asset_receivable', 'liability_payable'))
        partials = (terms.matched_debit_ids | terms.matched_credit_ids).sudo()
        return {'move': self.read(['name', 'state', 'date', 'invoice_date', 'company_id', 'currency_id',
                'journal_id', 'partner_id', 'amount_total', 'amount_total_signed', 'amount_residual',
                'amount_residual_signed', 'payment_state', 'invoice_currency_rate', 'posted_before',
                'secure_sequence_number', 'inalterable_hash'])[0],
            'terms': terms.read(['account_id', 'partner_id', 'currency_id', 'debit', 'credit', 'balance',
                'amount_currency', 'amount_residual', 'amount_residual_currency', 'date_maturity',
                'matched_debit_ids', 'matched_credit_ids', 'full_reconcile_id', 'matching_number']),
            'partials': partials.read([name for name, field in partials._fields.items() if field.store and name != 'display_name'])}

    def _eta_local_vat_preview_digest(self):
        self.ensure_one()
        return _digest({'financial': self._eta_local_vat_financial_snapshot(),
            'transport': self._eta_local_vat_transport_snapshot(),
            'header': self.read(['ref', 'invoice_origin', 'invoice_payment_term_id',
                'invoice_date_due', 'fiscal_position_id', 'invoice_cash_rounding_id', 'write_date'])[0],
            'lines': self.invoice_line_ids.read(['product_id', 'account_id', 'quantity', 'price_unit',
                'discount', 'tax_ids', 'currency_id', 'amount_currency', 'balance'])})

    def action_eta_local_vat_preview(self):
        self.ensure_one()
        tax = self._eta_local_vat_tax()
        self._eta_local_vat_validate(tax)
        wizard = self.env['tradeline.eta.local.vat.correction'].create({'move_id': self.id, 'tax_id': tax.id})
        return {'type': 'ir.actions.act_window', 'name': _('Preview local 14% VAT correction'),
            'res_model': wizard._name, 'res_id': wizard.id, 'view_mode': 'form', 'target': 'new'}

    def _eta_apply_local_vat(self, tax, expected_digest):
        self.ensure_one()
        self._eta_local_vat_check_access()
        with self.env.cr.savepoint():
            job = self.eta_job_ids.sudo()
            if job:
                self.env.cr.execute('SELECT pg_try_advisory_xact_lock(741180,%s)', [job.id])
                if not self.env.cr.fetchone()[0]:
                    raise UserError(_('This invoice is being checked by ETA. Try applying VAT again shortly.'))
            try:
                with self.env.cr.savepoint():
                    self.env.cr.execute('SELECT id FROM account_move WHERE id=%s FOR UPDATE NOWAIT', [self.id])
                    self.env.cr.execute('SELECT id FROM account_move_line WHERE move_id=%s ORDER BY id FOR UPDATE NOWAIT', [self.id])
            except psycopg2.errors.LockNotAvailable as exc:
                raise UserError(_('This invoice or its payment is being updated. Try applying VAT again shortly.')) from exc
            self.invalidate_recordset()
            job.invalidate_recordset()
            self.line_ids.invalidate_recordset()
            self._eta_local_vat_validate(tax)
            if self._eta_local_vat_preview_digest() != expected_digest:
                raise UserError(_('The invoice changed while VAT was being prepared. Try applying VAT again.'))
            plan = self._eta_local_vat_plan(tax)
            if not plan['commands']:
                raise UserError(_('The eligible invoice lines already have 14% VAT. No amendment is needed.'))
            if self.currency_id.compare_amounts(plan['total'], self.amount_total):
                raise UserError(_('This VAT selection changes the customer gross total. Local VAT correction must keep the invoice total, receivable and payments unchanged.'))
            financial = self._eta_local_vat_financial_snapshot()
            transport = self._eta_local_vat_transport_snapshot()
            before_tax = {'untaxed': self.amount_untaxed, 'tax': self.amount_tax, 'total': self.amount_total}
            # State changes are private to this savepoint. The normal reset and
            # posting methods deliberately never run: they detach ETA receipts,
            # remove payment reconciliations, or enqueue a new government send.
            editing = self.with_context(_eta_local_vat_edit=_LOCAL_VAT_EDIT,
                check_move_validity=False, tracking_disable=True, mail_notrack=True,
                skip_account_move_synchronization=True)
            editing.write({'state': 'draft'})
            editing.write({'invoice_line_ids': plan['commands']})
            editing.write({'state': 'posted'})
            self.flush_recordset()
            self.line_ids.flush_recordset()
            with self.with_context(check_move_validity=True)._check_balanced({'records': self}):
                pass
            self._check_fiscal_lock_dates()
            self.line_ids._check_tax_lock_date()
            self._validate_taxes_country()
            if (self._eta_local_vat_financial_snapshot() != financial
                    or self._eta_local_vat_transport_snapshot() != transport
                    or self.currency_id.compare_amounts(self.amount_untaxed, plan['untaxed'])
                    or self.currency_id.compare_amounts(self.amount_tax, plan['tax'])
                    or self.eta_vat_missing):
                raise UserError(_('The local VAT amendment could not preserve the original gross, payments or ETA evidence exactly. The entire amendment was rolled back.'))
            audit = self.env['ir.attachment'].with_context(_eta_local_vat_edit=_LOCAL_VAT_EDIT).create({'name': 'Odoo_local_VAT14_%s.json' % self.id,
                'res_model': 'account.move', 'res_id': self.id, 'company_id': self.company_id.id,
                'res_field': 'eta_local_vat_audit_file',
                'mimetype': 'application/json', 'public': False, 'raw': json.dumps({
                    'kind': 'local_accounting_vat14_only', 'move_id': self.id, 'invoice': self.name,
                    'user_id': self.env.uid, 'applied_utc': str(fields.Datetime.now()),
                    'preserved_eta_uuid': self.e_invoice_uuid or self.l10n_eg_uuid,
                    'before': before_tax, 'after': {'untaxed': self.amount_untaxed, 'tax': self.amount_tax, 'total': self.amount_total},
                    'financial_preservation_sha256': _digest(financial), 'eta_preservation_sha256': _digest(transport),
                    'no_eta_operation': True}, ensure_ascii=False).encode()})
            editing.write({'eta_local_vat_applied_at': fields.Datetime.now(),
                'eta_local_vat_applied_by': self.env.uid, 'eta_local_vat_audit_id': audit.id,
                'eta_local_vat_receipt_uuid': self.e_invoice_uuid or self.l10n_eg_uuid,
                'eta_local_vat_net_amount': self.amount_untaxed,
                'eta_local_vat_tax_amount': self.amount_tax,
                'eta_local_vat_gross_amount': self.amount_total})
            self.invalidate_recordset(['eta_local_vat_audit_file'])
            return True


class EtaLocalVatCorrection(models.TransientModel):
    _name = 'tradeline.eta.local.vat.correction'
    _description = 'Preview Odoo-only 14% VAT correction'
    _check_company_auto = True

    move_id = fields.Many2one('account.move', required=True, readonly=True, check_company=True)
    company_id = fields.Many2one(related='move_id.company_id')
    currency_id = fields.Many2one(related='move_id.currency_id')
    tax_id = fields.Many2one('account.tax', string='14% VAT to apply', required=True, readonly=True, check_company=True)
    original_untaxed = fields.Monetary(related='move_id.amount_untaxed', currency_field='currency_id')
    original_tax = fields.Monetary(string='Current tax amount', related='move_id.amount_tax', currency_field='currency_id')
    original_total = fields.Monetary(related='move_id.amount_total', currency_field='currency_id')
    preview_untaxed = fields.Monetary(compute='_compute_preview', currency_field='currency_id')
    preview_tax = fields.Monetary(compute='_compute_preview', currency_field='currency_id')
    preview_total = fields.Monetary(compute='_compute_preview', currency_field='currency_id')
    preview_difference = fields.Monetary(compute='_compute_preview', currency_field='currency_id')
    preview_note = fields.Text(compute='_compute_preview')
    can_apply = fields.Boolean(compute='_compute_preview')
    preview_fingerprint = fields.Char(readonly=True)
    applied_at = fields.Datetime(readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for values in vals_list:
            values = dict(values)
            if {'preview_fingerprint', 'applied_at'} & set(values):
                raise AccessError(_('The preview proof cannot be supplied by the client.'))
            move = self.env['account.move'].browse(values.get('move_id')).exists()
            tax = self.env['account.tax'].browse(values.get('tax_id')).exists()
            move.ensure_one()
            move._eta_local_vat_validate(tax)
            values['preview_fingerprint'] = move._eta_local_vat_preview_digest()
            prepared.append(values)
        return super().create(prepared)

    def write(self, vals):
        if {'move_id', 'tax_id', 'preview_fingerprint', 'applied_at'} & set(vals) \
                and self.env.context.get('_eta_local_vat_edit') is not _LOCAL_VAT_EDIT:
            raise AccessError(_('Open a new local VAT preview instead of changing its proof or invoice.'))
        return super().write(vals)

    @api.depends('move_id', 'move_id.amount_total', 'move_id.invoice_line_ids.tax_ids', 'tax_id', 'applied_at')
    def _compute_preview(self):
        for wizard in self:
            plan = wizard.move_id._eta_local_vat_plan(wizard.tax_id)
            wizard.preview_untaxed = plan['untaxed']
            wizard.preview_tax = plan['tax']
            wizard.preview_total = plan['total']
            wizard.preview_difference = plan['total']-wizard.move_id.amount_total
            wizard.can_apply = bool(plan['commands'] and plan['tax'] > 0
                and wizard.currency_id.is_zero(wizard.preview_difference) and not wizard.applied_at)
            wizard.preview_note = _('Odoo accounting only. The invoice stays posted; its customer gross total, payments, original ETA receipt and submission history stay unchanged. No credit, replacement, ETA request or resend is created.')

    def action_apply(self):
        self.ensure_one()
        self.check_access('write')
        if self.create_uid != self.env.user:
            raise AccessError(_('Open your own preview before applying a local VAT amendment.'))
        try:
            with self.env.cr.savepoint():
                self.env.cr.execute('SELECT id FROM tradeline_eta_local_vat_correction WHERE id=%s FOR UPDATE NOWAIT', [self.id])
        except psycopg2.errors.LockNotAvailable as exc:
            raise UserError(_('This VAT preview is already being applied. Wait for the invoice to refresh.')) from exc
        self.invalidate_recordset()
        if not self.applied_at:
            self.move_id._eta_apply_local_vat(self.tax_id, self.preview_fingerprint)
            self.with_context(_eta_local_vat_edit=_LOCAL_VAT_EDIT).write({'applied_at': fields.Datetime.now()})
        return {'type': 'ir.actions.act_window', 'res_model': 'account.move',
            'res_id': self.move_id.id, 'view_mode': 'form', 'views': [(False, 'form')]}


class EtaLocalVatLegacyWizardGuard(models.TransientModel):
    _inherit = 'tradeline.eta.vat.correction'

    def action_prepare(self):
        self.ensure_one()
        if (self.move_id.e_invoice_status == 'Valid' and self.move_id._eta_local_vat_has_history()
                and self.env.context.get('_eta_legacy_credit_route') is not _LEGACY_CREDIT_ROUTE):
            return self.move_id.action_eta_local_vat_preview()
        return super().action_prepare()

    def _receipt(self):
        receipt, document = super()._receipt()
        if (receipt.get('status') == 'Valid'
                and self.env.context.get('_eta_legacy_credit_route') is not _LEGACY_CREDIT_ROUTE):
            raise UserError(_('ETA now confirms this invoice is approved. Refresh its ETA status, then open the local 14% VAT preview. No credit note, replacement, or resend was created.'))
        return receipt, document


class EtaLocalVatAuditAttachment(models.Model):
    _inherit = 'ir.attachment'

    @api.model_create_multi
    def create(self, vals_list):
        if (self.env.context.get('_eta_local_vat_edit') is not _LOCAL_VAT_EDIT
                and any(vals.get('res_model') == 'account.move'
                    and vals.get('res_field') == 'eta_local_vat_audit_file' for vals in vals_list)):
            raise AccessError(_('Local VAT amendment evidence can only be created by the verified amendment.'))
        return super().create(vals_list)

    def write(self, vals):
        if (self.env.context.get('_eta_local_vat_edit') is not _LOCAL_VAT_EDIT
                and self.sudo().filtered(lambda item: item.res_model == 'account.move'
                    and item.res_field == 'eta_local_vat_audit_file')):
            raise AccessError(_('The local VAT amendment evidence is immutable.'))
        return super().write(vals)

    def unlink(self):
        if (self.env.context.get('_eta_local_vat_edit') is not _LOCAL_VAT_EDIT
                and self.sudo().filtered(lambda item: item.res_model == 'account.move'
                    and item.res_field == 'eta_local_vat_audit_file')):
            raise AccessError(_('The local VAT amendment evidence is immutable.'))
        return super().unlink()

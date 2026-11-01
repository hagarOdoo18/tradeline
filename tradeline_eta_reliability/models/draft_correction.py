"""VAT corrections with preserved authority receipts and transport evidence."""
import json
from collections import defaultdict
from datetime import timedelta

import psycopg2
import requests

from odoo import api, fields, models, Command, _
from odoo.exceptions import AccessError, UserError

from .client import EtaClient, EtaError
from .submission_safety import taxpayer

_VERIFIED_RESET = object()  # Internal proof; cannot be supplied through an RPC context.


class AccountMoveDraftCorrection(models.Model):
    _inherit = 'account.move'

    eta_vat_original_id = fields.Many2one('account.move', string='VAT correction of', copy=False, index=True,
                                         check_company=True, readonly=True)
    eta_vat_correction_ids = fields.One2many('account.move', 'eta_vat_original_id', string='VAT correction documents')

    def action_eta_correct_vat(self):
        self.ensure_one()
        self.check_access('write')
        if self.state != 'posted' or self.move_type != 'out_invoice':
            raise UserError(_('Select a posted customer invoice to correct VAT.'))
        return {'type': 'ir.actions.act_window', 'name': _('Correct VAT'),
                'res_model': 'tradeline.eta.vat.correction', 'view_mode': 'form', 'target': 'new',
                'context': {'default_move_id': self.id}}

    def action_eta_vat_documents(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('VAT correction documents'),
                'res_model': 'account.move', 'view_mode': 'list,form',
                'domain': [('id', 'in', self.eta_vat_correction_ids.ids)]}

    def _check_extra_fields(self, invoice_params):
        invoice_params = super()._check_extra_fields(invoice_params)
        if self.move_type == 'out_refund' and self.e_invoice_credit_src:
            invoice_params['references'] = [self.e_invoice_credit_src]
        return invoice_params

    def _post(self, soft=True):
        for move in self.filtered(lambda record: record.eta_vat_original_id and record.move_type == 'out_invoice'):
            prerequisites = move.eta_vat_original_id.eta_vat_correction_ids.filtered(
                lambda record: record.move_type in ('out_refund', 'entry') and record.state != 'cancel')
            if not prerequisites.filtered(lambda record: record.move_type == 'out_refund'):
                raise UserError(_('The original VAT correction credit note is missing. Reconcile the correction documents first.'))
            if any(record.state != 'posted' and record not in self for record in prerequisites):
                raise UserError(_('Post the VAT correction credit note and any accounting adjustment before the replacement invoice.'))
            credits = prerequisites.filtered(lambda record: record.move_type == 'out_refund')
            if any(record.e_invoice_status != 'Valid' or record.eta_job_ids.sudo().state != 'valid'
                   or not record.e_invoice_uuid for record in credits):
                raise UserError(_('Wait until the VAT correction credit note is Valid at ETA before posting the replacement invoice.'))
        return super()._post(soft=soft)

    def _eta_controls_draft_reset(self):
        self.ensure_one()
        return self.move_type in ('out_invoice', 'out_refund') and bool(
            self.eta_job_ids.sudo() or self.company_id.sudo().client_id
            or self.edi_document_ids.filtered(lambda doc: doc.edi_format_id.code == 'eg_eta'))

    def _eta_can_reset_unsent(self):
        self.ensure_one()
        job = self.eta_job_ids.sudo()
        if (self.e_invoice_sent or self.e_invoice_uuid or self.l10n_eg_uuid
                or self.l10n_eg_submission_number or self.l10n_eg_eta_json_doc_id
                or self.e_invoice_status in ('Submitted', 'Valid')):
            return False
        if job and (job.state not in ('queued', 'blocked', 'invalid')
                or job.historical_review or job.submission_uuid
                or job.duplicate_retry_at or job.duplicate_payload_hash
                or (job.attempted_at or job.attempts) and not job.safe_to_retry):
            return False
        if not job and (self.e_invoice_json or self.invoice_signed
                or self.edi_document_ids.filtered(
                    lambda d: d.edi_format_id.code == 'eg_eta' and d.state in ('sent', 'to_cancel'))):
            return False
        # Legacy resets sometimes erased both active pointers. A retained
        # authority attachment still requires the verified correction route.
        if isinstance(self.id, int) and self.env.context.get('_eta_verified_reset') is not _VERIFIED_RESET:
            attachments = self.env['ir.attachment'].sudo().search([
                ('res_model', '=', 'account.move'), ('res_id', '=', self.id),
                ('name', '=like', 'ETA_%.json'), ('mimetype', '=', 'application/json')])
            for attachment in attachments:
                try:
                    saved = json.loads(attachment.raw)
                    if (saved.get('response', {}).get('l10n_eg_uuid')
                            and saved.get('request', {}).get('internalID') == self.name):
                        return False
                except (ValueError, TypeError, AttributeError):
                    continue
        return True

    def _eta_cancelled_reset_candidate(self):
        self.ensure_one()
        job = self.eta_job_ids.sudo()
        native = self.edi_document_ids.filtered(lambda doc: doc.edi_format_id.code == 'eg_eta')
        return bool((self.e_invoice_uuid or self.l10n_eg_uuid)
            and (self.e_invoice_status == 'Cancelled' or native and all(doc.state == 'cancelled' for doc in native))
            and not (job and (job.state == 'unknown' or job.duplicate_retry_at or job.duplicate_payload_hash)))

    def _check_edi_documents_for_reset_to_draft(self):
        if not self._eta_controls_draft_reset():
            return super()._check_edi_documents_for_reset_to_draft()
        if not (self._eta_can_reset_unsent() or self._eta_cancelled_reset_candidate()):
            return False
        # The external queue owns ETA transport. A stale native sent/to_cancel
        # flag alone is not proof of submission; other EDI formats keep their rules.
        for doc in self.edi_document_ids.filtered(lambda d: d.edi_format_id.code != 'eg_eta'):
            applicability = doc.edi_format_id._get_move_applicability(self)
            if (doc.edi_format_id._needs_web_services() and doc.state in ('sent', 'to_cancel')
                    and applicability and applicability.get('cancel')):
                return False
        return True

    def _edi_allow_button_draft(self):
        if self._eta_controls_draft_reset():
            return self._check_edi_documents_for_reset_to_draft()
        return super()._edi_allow_button_draft()

    @api.depends('edi_document_ids.state', 'eta_job_ids.state', 'eta_job_ids.attempted_at',
                 'eta_job_ids.attempts', 'eta_job_ids.safe_to_retry', 'eta_job_ids.submission_uuid',
                 'eta_job_ids.duplicate_retry_at', 'eta_job_ids.duplicate_payload_hash',
                 'eta_job_ids.historical_review', 'e_invoice_sent', 'e_invoice_uuid',
                 'e_invoice_status', 'e_invoice_json', 'invoice_signed', 'l10n_eg_uuid',
                 'l10n_eg_submission_number', 'l10n_eg_eta_json_doc_id', 'company_id.client_id', 'move_type')
    def _compute_show_reset_to_draft_button(self):
        super()._compute_show_reset_to_draft_button()
        for move in self:
            if move._eta_controls_draft_reset() and not (move._eta_can_reset_unsent() or move._eta_cancelled_reset_candidate()):
                move.show_reset_to_draft_button = False

    def button_draft(self):
        self.check_access('write')
        controlled = self.filtered(lambda move: move._eta_controls_draft_reset()).sorted('id')
        cancelled = self.env['account.move']
        for move in controlled:
            job = move.eta_job_ids.sudo()
            if job:
                self.env.cr.execute('SELECT pg_try_advisory_xact_lock(741180, %s)', [job.id])
                if not self.env.cr.fetchone()[0]:
                    raise UserError(_('This invoice is being processed by ETA. Retry Reset to Draft shortly.'))
            try:
                with self.env.cr.savepoint():
                    self.env.cr.execute('SELECT id FROM account_move WHERE id=%s FOR UPDATE NOWAIT', [move.id])
            except psycopg2.errors.LockNotAvailable as exc:
                raise UserError(_('This invoice is being updated. Retry Reset to Draft shortly.')) from exc
            move.invalidate_recordset()
            job.invalidate_recordset()
            if not move._eta_can_reset_unsent():
                if move._eta_cancelled_reset_candidate():
                    uuid = move.e_invoice_uuid or move.l10n_eg_uuid
                    client = EtaClient(move.company_id.sudo())
                    try:
                        receipt = client.request('GET', '/api/v1.0/documents/%s/raw' % uuid)
                    finally:
                        client.close()
                    if (receipt.get('uuid') != uuid or receipt.get('internalId') != move.name
                            or receipt.get('status') != 'Cancelled'):
                        raise UserError(_('ETA has not confirmed cancellation of this invoice. Its receipt is preserved.'))
                    self.env['ir.attachment'].sudo().create({
                        'name': 'ETA_cancelled_before_draft_%s.json' % uuid, 'res_model': 'account.move',
                        'res_id': move.id, 'company_id': move.company_id.id, 'mimetype': 'application/json',
                        'raw': json.dumps(receipt, ensure_ascii=False).encode()})
                    cancelled |= move
                else:
                    raise UserError(_('This invoice has an ETA receipt or an unresolved submission. Refresh ETA status first. '
                                      'Use Correct VAT to prepare a credit note and VAT replacement for an accepted invoice.'))
            if not move._check_edi_documents_for_reset_to_draft():
                raise UserError(_('Another electronic invoicing service requires cancellation before this invoice can be reset.'))
        # Check the entire selection before the legacy implementation clears ETA fields.
        for move in controlled:
            job = move.eta_job_ids.sudo()
            if job:
                evidence = job.read(['state', 'payload', 'payload_hash', 'attempted_at', 'attempts',
                                     'safe_to_retry', 'last_error', 'historical_fingerprint'])[0]
                self.env['ir.attachment'].sudo().create({
                    'name': 'ETA_before_draft_%s.json' % job.id, 'res_model': 'account.move',
                    'res_id': move.id, 'company_id': move.company_id.id, 'mimetype': 'application/json',
                    'raw': json.dumps(evidence, default=str, ensure_ascii=False).encode(),
                })
                if move in cancelled:
                    job.write({'state': 'blocked', 'safe_to_retry': True, 'submission_uuid': False,
                               'historical_review': False})
            move.edi_document_ids.filtered(lambda d: d.edi_format_id.code == 'eg_eta').write({
                'state': 'to_send', 'error': False, 'blocking_level': False})
        reset_moves = self.with_context(_eta_verified_reset=_VERIFIED_RESET) if cancelled else self
        result = super(AccountMoveDraftCorrection, reset_moves).button_draft()
        for move in controlled:
            move.write({'eta_submission_pending': False, 'e_invoice_long_id': False, 'e_invoice_issue': False})
            move.eta_job_ids.sudo().write({
                'state': 'queued', 'payload': False, 'payload_hash': False, 'attempted_at': False,
                'safe_to_retry': False, 'first_submission_candidate': False,
                'next_attempt': False, 'last_error': False, 'failures': 0, 'attempts': 0,
                'historical_fingerprint': False, 'historical_source_hash': False,
                'historical_reviewed_at': False, 'historical_submission_verified': False,
                'authority_receipt_hash': False, 'current_issue_datetime': False,
            })
            if move in cancelled:
                move.eta_job_ids.sudo().write({'state': 'blocked', 'submission_uuid': False,
                    'last_error': _('The original ETA document was cancelled. Issue a new invoice for a replacement; '
                                    'this cancelled document is not queued for resubmission.')})
        return result


class EtaVatCorrection(models.TransientModel):
    _name = 'tradeline.eta.vat.correction'
    _description = 'Correct Invoice VAT'
    _check_company_auto = True

    move_id = fields.Many2one('account.move', required=True, readonly=True, check_company=True)
    company_id = fields.Many2one(related='move_id.company_id', readonly=True)
    tax_id = fields.Many2one('account.tax', string='Correct VAT', required=True, check_company=True,
        domain="[('company_id', '=', company_id), ('type_tax_use', '=', 'sale'), ('amount_type', '=', 'percent'), ('amount', '>', 0), ('type_code_id.code', '=', 'T1')]")
    adjustment_journal_id = fields.Many2one('account.journal', string='Adjustment journal', check_company=True,
        domain="[('company_id', '=', company_id), ('type', '=', 'general')]")
    reason = fields.Char(required=True, default='Correct missing VAT')

    def _lock_invoice(self):
        move = self.move_id
        move.check_access('write')
        job = move.eta_job_ids.sudo()
        if job:
            self.env.cr.execute('SELECT pg_try_advisory_xact_lock(741180, %s)', [job.id])
            if not self.env.cr.fetchone()[0]:
                raise UserError(_('ETA is processing this invoice. Retry Correct VAT shortly.'))
        try:
            with self.env.cr.savepoint():
                self.env.cr.execute('SELECT id FROM account_move WHERE id=%s FOR UPDATE NOWAIT', [move.id])
        except psycopg2.errors.LockNotAvailable as exc:
            raise UserError(_('This invoice is being updated. Retry Correct VAT shortly.')) from exc
        move.invalidate_recordset()
        job.invalidate_recordset()
        if (job and (job.state == 'unknown' or job.duplicate_retry_at or job.duplicate_payload_hash
                or job.state not in ('valid', 'accepted', 'invalid')
                and (job.attempted_at or job.attempts) and not job.safe_to_retry)):
            raise UserError(_('An ETA submission is unresolved. Reconcile its status before preparing a correction.'))

    def _receipt(self):
        """Read the actual authority receipt, including links erased by legacy resets."""
        move = self.move_id
        uuid = move.e_invoice_uuid or move.l10n_eg_uuid
        if not uuid:
            attachments = self.env['ir.attachment'].sudo().search([
                ('res_model', '=', 'account.move'), ('res_id', '=', move.id),
                ('name', '=like', 'ETA_%.json'), ('mimetype', '=', 'application/json')])
            uuids = set()
            for attachment in attachments:
                try:
                    saved = json.loads(attachment.raw)
                    known = saved.get('response', {}).get('l10n_eg_uuid')
                    if known and saved.get('request', {}).get('internalID') == move.name:
                        uuids.add(known)
                except (ValueError, TypeError, AttributeError):
                    continue
            if len(uuids) == 1:
                uuid = uuids.pop()
        if not uuid:
            raise UserError(_('The original ETA receipt must be reconciled before correcting this invoice. No new document was created.'))
        client = EtaClient(move.company_id.sudo())
        try:
            receipt = client.request('GET', '/api/v1.0/documents/%s/raw' % uuid)
        except (EtaError, requests.RequestException) as exc:
            raise UserError(_('ETA could not confirm the original receipt: %s', str(exc))) from exc
        finally:
            client.close()
        document = receipt.get('document')
        if isinstance(document, str):
            try:
                document = json.loads(document)
            except ValueError as exc:
                raise UserError(_('ETA returned an unreadable original document. Retry status verification before correcting VAT.')) from exc
        if (receipt.get('uuid') != uuid or receipt.get('internalId') != move.name
                or not isinstance(document, dict) or document.get('internalID') != move.name
                or taxpayer(document.get('issuer', {}).get('id')) != taxpayer(move.company_id.vat)):
            raise UserError(_('The authority receipt does not match this invoice and company. Reconcile its identity first.'))
        self.env['ir.attachment'].sudo().create({
            'name': 'ETA_VAT_correction_receipt_%s.json' % uuid, 'res_model': 'account.move',
            'res_id': move.id, 'company_id': move.company_id.id, 'mimetype': 'application/json',
            'raw': json.dumps(receipt, ensure_ascii=False).encode()})
        return receipt, document

    def _apply_vat(self, move):
        for line in move.invoice_line_ids.filtered(lambda line: line.display_type == 'product'):
            line.tax_ids = (line.tax_ids.filtered(lambda tax: tax.type_code_id.code != 'T1'
                and not (tax.l10n_eg_eta_code or '').startswith('T1_')) | self.tax_id)

    def _restore_credit_lines(self, credit, document):
        """Reverse the submitted values, even if Odoo taxes were edited later."""
        move = self.move_id
        if move.currency_id != self.env.ref('base.EGP') or move.company_id.currency_id != move.currency_id:
            raise UserError(_('Foreign-currency VAT corrections need an accountant to verify the original exchange-rate treatment.'))
        source = move.invoice_line_ids.filtered(lambda line: line.product_id and line.quantity)
        target = credit.invoice_line_ids.filtered(lambda line: line.product_id and line.quantity)
        old_lines = document.get('invoiceLines', [])
        if len(source) != len(old_lines) or len(target) != len(source):
            raise UserError(_('The current products do not match the original ETA lines. Finance must reconcile the source first.'))
        for original, line, old in zip(source, target, old_lines):
            if (original.product_id.e_invoicing_code != old.get('itemCode')
                    or abs(original.quantity - float(old.get('quantity', 0))) > .000001):
                raise UserError(_('The original ETA products or quantities differ. Finance must reconcile them first.'))
            taxes = self.env['account.tax']
            included = 0
            for item in old.get('taxableItems', []):
                if not item.get('amount') and not item.get('rate'):
                    continue
                candidates = self.env['account.tax'].search([
                    ('company_id', '=', move.company_id.id), ('type_tax_use', '=', 'sale'),
                    ('amount_type', '=', 'percent'), ('type_code_id.code', '=', item.get('taxType')),
                    ('sub_type_code_id.code', '=', item.get('subType'))])
                rate = float(item.get('rate', 0))
                candidates = candidates.filtered(lambda tax: abs(abs(tax.amount) - rate) < .000001
                    and (tax.amount < 0 if item.get('taxType') == 'T4' else tax.amount >= 0))
                preferred = candidates & original.tax_ids
                selected = preferred[:1] or candidates[:1]
                if not selected:
                    raise UserError(_('Configure a company sales tax matching the original ETA tax %s/%s before correcting it.',
                                      item.get('taxType'), item.get('subType')))
                taxes |= selected
                if selected.price_include:
                    included += float(item['amount']) * (-1 if selected.amount < 0 else 1)
            qty = float(old['quantity'])
            discount = float(old.get('discount', {}).get('rate', 0))
            if qty <= 0 or discount >= 100:
                raise UserError(_('Finance must review non-positive quantities or fully discounted ETA correction lines.'))
            unit = (float(old['netTotal']) + included) / qty / (1 - discount / 100)
            line.write({'quantity': qty, 'discount': discount, 'price_unit': unit,
                        'tax_ids': [Command.set(taxes.ids)]})
        if abs(credit.amount_total - float(document['totalAmount'])) >= .01:
            raise UserError(_('The credit note does not match the original ETA total. Finance must verify its tax mapping.'))

    def _alignment_entry(self, credit):
        """Account for edits already made to Odoo after the original ETA submission.

        Original + alignment + authority-value credit must net to zero. All
        entries remain draft for review; no ledger or government POST occurs.
        """
        move = self.move_id
        groups = defaultdict(lambda: {'balance': 0, 'base': 0})
        for line in (move.line_ids | credit.line_ids).filtered(lambda line: line.display_type not in ('line_section', 'line_note')):
            key = (line.account_id.id, line.partner_id.id, line.tax_repartition_line_id.id,
                   tuple(sorted(line.tax_tag_ids.ids)), tuple(sorted(line.tax_ids.ids)),
                   line.tax_tag_invert, json.dumps(line.analytic_distribution or {}, sort_keys=True))
            groups[key]['balance'] += line.balance
            groups[key]['base'] += line.tax_base_amount
        commands = []
        for (account_id, partner_id, repartition_id, tags, taxes, invert, analytics), amounts in groups.items():
            balance = -move.company_id.currency_id.round(amounts['balance'])
            if not balance:
                continue
            account = self.env['account.account'].browse(account_id)
            if account.account_type in ('asset_receivable', 'liability_payable'):
                raise UserError(_('The Odoo and ETA receivable amounts differ. Finance must reconcile payments before a VAT replacement.'))
            commands.append(Command.create({'name': self.reason, 'account_id': account_id,
                'partner_id': partner_id, 'debit': max(balance, 0), 'credit': max(-balance, 0),
                'tax_repartition_line_id': repartition_id, 'tax_tag_ids': [Command.set(tags)],
                'tax_ids': [Command.set(taxes)], 'tax_base_amount': -amounts['base'],
                'tax_tag_invert': invert, 'analytic_distribution': json.loads(analytics)}))
        if not commands:
            return self.env['account.move']
        journal = self.adjustment_journal_id or self.env['account.journal'].search([
            ('company_id', '=', move.company_id.id), ('type', '=', 'general')], limit=1)
        if not journal or journal.company_id != move.company_id or journal.type != 'general':
            raise UserError(_('Select a general journal for the adjustment of the earlier Odoo tax edit.'))
        return self.env['account.move'].with_context(skip_invoice_sync=True).create({'move_type': 'entry', 'journal_id': journal.id,
            'date': fields.Date.context_today(self), 'ref': _('ETA VAT alignment: %s', move.name),
            'eta_vat_original_id': move.id, 'line_ids': commands})

    def action_prepare(self):
        self.ensure_one()
        if not (self.env.is_superuser() or self.env.user.has_group('account.group_account_manager')):
            raise AccessError(_('An accounting manager must prepare an ETA VAT correction.'))
        move = self.move_id.with_company(self.company_id)
        if (move.state != 'posted' or move.move_type != 'out_invoice'
                or self.tax_id.company_id != move.company_id or self.tax_id.type_tax_use != 'sale'
                or self.tax_id.type_code_id.code != 'T1' or self.tax_id.amount <= 0
                or self.tax_id.amount_type != 'percent' or not self.tax_id.active):
            raise UserError(_('Select a posted customer invoice and a positive VAT sales tax from the same company.'))
        self._lock_invoice()
        existing = move.eta_vat_correction_ids.filtered(lambda record: record.state != 'cancel')
        if existing:
            return move.action_eta_vat_documents()
        if move._eta_can_reset_unsent():
            move.button_draft()
            self._apply_vat(move)
            return {'type': 'ir.actions.act_window', 'res_model': 'account.move',
                    'view_mode': 'form', 'res_id': move.id}
        receipt, document = self._receipt()
        status = receipt.get('status')
        if status in ('Valid', 'Invalid'):
            move._eta_enqueue()
        if status == 'Invalid':
            # A fresh matching rejection is the only accepted-receipt reset route.
            # Retain the full government response before replacing active pointers.
            move.write({'e_invoice_uuid': False, 'e_invoice_sent': False, 'e_invoice_status': 'Draft',
                        'e_invoice_long_id': False, 'l10n_eg_eta_json_doc_id': False})
            move.eta_job_ids.sudo().write({'state': 'blocked', 'safe_to_retry': True, 'submission_uuid': False,
                'historical_review': False})
            move.with_context(_eta_verified_reset=_VERIFIED_RESET).button_draft()
            self._apply_vat(move)
            move.eta_job_ids.sudo().write({'state': 'review', 'historical_review': True})
            return {'type': 'ir.actions.act_window', 'res_model': 'account.move', 'view_mode': 'form', 'res_id': move.id}
        if status != 'Valid':
            raise UserError(_('ETA reports %s. Wait for final validation or reconcile the cancellation before correcting VAT.', status))
        if move.reversal_move_ids.filtered(lambda record: record.state == 'posted'):
            raise UserError(_('This invoice already has a posted credit note. Finance must reconcile it before creating a full VAT replacement.'))
        move.eta_job_ids.sudo()._apply_remote(receipt)
        # Preserve the original and prepare the government-supported credit and
        # replacement route. These drafts require normal review and posting.
        credit = move._reverse_moves([{'date': fields.Date.context_today(self),
            'invoice_date': fields.Date.context_today(self), 'auto_post': 'no',
            'ref': self.reason, 'eta_vat_original_id': move.id,
            'e_invoice_credit_src': receipt['uuid']}], cancel=False)
        self._restore_credit_lines(credit, document)
        replacement = move.with_context(include_business_fields=True).copy({'name': '/', 'state': 'draft', 'invoice_date': fields.Date.context_today(self),
            'date': fields.Date.context_today(self), 'auto_post': 'no', 'ref': self.reason,
            'eta_vat_original_id': move.id, 'e_invoice_credit_src': False,
            'invoice_origin': move.name})
        self._apply_vat(replacement)
        alignment = self._alignment_entry(credit)
        move.message_post(body=_('VAT correction drafts prepared: credit note matching ETA receipt %(uuid)s, '
                                'replacement invoice with VAT, and %(count)s accounting adjustment. '
                                'Review and post the drafts to submit the correction to ETA.',
                                uuid=receipt['uuid'], count=len(alignment)))
        return move.action_eta_vat_documents()


class EtaVatCorrectionTransport(models.Model):
    _inherit = 'tradeline.eta.job'

    def _preflight_submission(self, client):
        move = self.move_id
        if move.move_type == 'out_invoice' and move.eta_vat_original_id:
            credit = move.eta_vat_original_id.eta_vat_correction_ids.filtered(
                lambda record: record.move_type == 'out_refund' and record.state != 'cancel')
            if not credit or any(record.state != 'posted' or record.e_invoice_status != 'Valid'
                                 or record.eta_job_ids.sudo().state != 'valid' or not record.e_invoice_uuid
                                 for record in credit):
                self.write({'next_attempt': fields.Datetime.now() + timedelta(minutes=2),
                    'last_error': _('Waiting for the linked VAT correction credit note to be Valid at ETA. '
                                    'Post and sync that credit note first; the replacement has not been sent.')})
                return False
            for record in credit:
                receipt = client.request('GET', '/api/v1.0/documents/%s/raw' % record.e_invoice_uuid)
                if (receipt.get('uuid') != record.e_invoice_uuid or receipt.get('internalId') != record.name
                        or receipt.get('status') != 'Valid'):
                    raise UserError(_('ETA no longer confirms the VAT correction credit note as Valid. Reconcile it before sending the replacement.'))
        return super()._preflight_submission(client)

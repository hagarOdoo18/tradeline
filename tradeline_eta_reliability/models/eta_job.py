import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone

import psycopg2
import pytz
import requests

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError

from .client import EtaClient, EtaError
from .processing_window import EtaStopped, check_processing_window

_logger = logging.getLogger(__name__)
STATES = [('queued', 'Queued'), ('unknown', 'Checking uncertain submission'),
          ('accepted', 'Awaiting ETA validation'), ('valid', 'Valid'),
          ('invalid', 'Rejected by ETA'), ('blocked', 'Needs correction'),
          ('review', 'Historical verification queued')]
PROCESSING_REVISION = '18.0.1.0.20'


class EtaJob(models.Model):
    _name = 'tradeline.eta.job'
    _description = 'ETA Submission Job'
    _order = 'next_attempt, id'
    _rec_name = 'move_id'

    move_id = fields.Many2one('account.move', required=True, ondelete='restrict', index=True)
    company_id = fields.Many2one(related='move_id.company_id', store=True, index=True)
    state = fields.Selection(STATES, required=True, default='queued', index=True)
    attempts = fields.Integer(readonly=True)
    failures = fields.Integer(readonly=True)
    safe_to_retry = fields.Boolean(readonly=True)
    first_submission_candidate = fields.Boolean(readonly=True)
    next_attempt = fields.Datetime(index=True, readonly=True)
    attempted_at = fields.Datetime(readonly=True)
    checked_at = fields.Datetime(readonly=True)
    last_error = fields.Text(readonly=True)
    payload = fields.Text(readonly=True, groups='account.group_account_manager')
    payload_hash = fields.Char(readonly=True)
    submission_uuid = fields.Char(readonly=True)
    processing_revision = fields.Char(readonly=True)
    duplicate_retry_at = fields.Datetime(readonly=True)
    duplicate_payload_hash = fields.Char(readonly=True)
    omit_optional_receiver_id = fields.Boolean(readonly=True,
        help='Omit an authority-rejected optional personal ID only below EGP 25,000. The customer record is preserved.')
    issue_date_policy = fields.Selection([
        ('invoice', 'Original invoice date'), ('current', 'Approved current ETA issue date')],
        default='invoice', required=True, readonly=True)
    current_issue_datetime = fields.Datetime(string='ETA issue time', readonly=True)
    issue_date_approved_by = fields.Many2one('res.users', string='Date treatment approved by', readonly=True)
    issue_date_approved_at = fields.Datetime(string='Date treatment approved on', readonly=True)
    issue_date_approval_note = fields.Text(string='Date treatment approval', readonly=True)
    _sql_constraints = [('move_unique', 'unique(move_id)', 'An invoice has one ETA submission job.')]

    def _effective_issue_date(self):
        self.ensure_one()
        if self.issue_date_policy == 'current':
            if not (self.issue_date_approved_by and self.issue_date_approved_at and self.issue_date_approval_note):
                raise UserError(_('Approve the current ETA issue date before preparing this document.'))
            return fields.Datetime.context_timestamp(
                self.with_context(tz='Africa/Cairo'), fields.Datetime.now()).date()
        return self.move_id.invoice_date

    def action_approve_current_issue_date(self):
        """Explicit date treatment for a document known not to be accepted at ETA."""
        if not (self.env.is_superuser() or self.env.user.has_group('account.group_account_manager')
                or self.env.user.has_group('base.group_system')):
            raise AccessError(_('An accounting manager must approve the ETA issue-date treatment.'))
        self.check_access('write')
        for job in self:
            move = job.move_id
            move.check_access('write')
            self.env.cr.execute('SELECT pg_try_advisory_xact_lock(741180,%s)', [job.id])
            if not self.env.cr.fetchone()[0]:
                raise UserError(_('This invoice is being processed. Refresh its status before approving a retry.'))
            self.env.cr.execute('SELECT id FROM account_move WHERE id=%s FOR UPDATE NOWAIT', [move.id])
            job.invalidate_recordset(); move.invalidate_recordset()
            if (job.state not in ('queued', 'blocked') or move.state != 'posted'
                    or move.move_type not in ('out_invoice', 'out_refund')
                    or move.e_invoice_uuid or move.e_invoice_sent or move.l10n_eg_uuid
                    or move.l10n_eg_eta_json_doc_id or job.attempted_at and not job.safe_to_retry):
                raise UserError(_('Only an unsent or explicitly rejected document without an existing ETA receipt can use this action. Reconcile accepted, invalid or uncertain receipts first.'))
            if self.env['account.move'].sudo().search_count([
                    ('company_id', '=', move.company_id.id), ('name', '=', move.name),
                    ('state', '=', 'posted'), ('move_type', 'in', ('out_invoice', 'out_refund'))]) != 1:
                raise UserError(_('The invoice number is not unique within its company. Reconcile it before approving a new ETA document.'))
            approval = _('Current ETA issue date approved. Preserve the original Odoo invoice date and accounting values.')
            history = {'invoice_date': str(move.invoice_date), 'old_payload': job.payload,
                       'old_payload_hash': job.payload_hash, 'old_attempted_at': str(job.attempted_at or ''),
                       'old_error': job.last_error, 'previous_policy': job.issue_date_policy,
                       'approval': approval, 'approved_by': self.env.uid, 'approved_at': str(fields.Datetime.now())}
            self.env['ir.attachment'].sudo().create({
                'name': 'ETA_issue_date_approval_%s.json' % job.id, 'res_model': 'account.move',
                'res_id': move.id, 'company_id': move.company_id.id, 'mimetype': 'application/json',
                'raw': json.dumps(history, ensure_ascii=False).encode(),
            })
            job.write({'issue_date_policy': 'current', 'current_issue_datetime': False,
                       'issue_date_approved_by': self.env.uid, 'issue_date_approved_at': fields.Datetime.now(),
                       'issue_date_approval_note': approval, 'state': 'queued', 'payload': False,
                       'payload_hash': False, 'attempted_at': False, 'next_attempt': False,
                       'last_error': False, 'failures': 0, 'safe_to_retry': False,
                       'first_submission_candidate': False})
        return True

    def _delay(self, error, seconds=None, state=None):
        self.ensure_one()
        self.write({'failures': self.failures + 1, 'next_attempt': fields.Datetime.now() + timedelta(
            seconds=seconds or min(3600, 60 * 2 ** min(self.failures, 6))),
            'last_error': str(error)[:500], **({'state': state} if state else {})})

    def _apply_remote(self, data):
        """Only accept the authority's identity-matched status; never invent success."""
        self.ensure_one()
        move = self.move_id
        uuid = data.get('uuid')
        if not uuid or data.get('internalId') != move.name:
            raise EtaError('ETA document identity does not match this invoice.')
        if move.e_invoice_uuid and move.e_invoice_uuid != uuid:
            raise EtaError('ETA returned a different document UUID; review is required.')
        if move.l10n_eg_uuid and move.l10n_eg_uuid != uuid:
            raise EtaError('The native ETA UUID differs; review both submissions before continuing.')
        status = data.get('status')
        if status not in ('Submitted', 'Valid', 'Invalid', 'Rejected', 'Cancelled'):
            raise EtaError('ETA did not return a recognized document status.')
        values = {'e_invoice_uuid': uuid, 'e_invoice_status': status,
                  'e_invoice_sent': True, 'eta_submission_pending': False}
        if not move.e_invoice_date:
            received = data.get('dateTimeReceived')
            try:
                received = datetime.fromisoformat(received.replace('Z', '+00:00')) if received else None
                if received and received.tzinfo:
                    received = received.astimezone(timezone.utc).replace(tzinfo=None)
            except (ValueError, TypeError):
                received = None
            if received or self.attempted_at:
                values['e_invoice_date'] = received or self.attempted_at
        if data.get('longId'):
            values['e_invoice_long_id'] = data['longId']
        move.write(values)
        self.write({'state': 'valid' if status == 'Valid' else 'accepted' if status == 'Submitted' else 'invalid',
                    'submission_uuid': data.get('submissionUUID') or self.submission_uuid,
                    'checked_at': fields.Datetime.now(), 'last_error': False, 'failures': 0,
                    'next_attempt': fields.Datetime.now() + timedelta(minutes=2) if status == 'Submitted' else False,
                    'duplicate_retry_at': False, 'duplicate_payload_hash': False})
        native = move.edi_document_ids.filtered(lambda d: d.edi_format_id.code == 'eg_eta' and d.state == 'to_send')
        if status == 'Valid':
            # Retain the government response as evidence; no synthetic signed document.
            document = data.get('document')
            if isinstance(document, str):
                try:
                    document = json.loads(document)
                except ValueError:
                    document = None
            if isinstance(document, dict):
                attachment = self.env['ir.attachment'].create({
                    'name': 'ETA_%s.json' % uuid, 'res_model': 'account.move', 'res_id': move.id,
                    'company_id': move.company_id.id,
                    'mimetype': 'application/json', 'raw': json.dumps({'request': document, 'response': {
                        'l10n_eg_uuid': uuid, 'l10n_eg_long_id': data.get('longId'),
                        'l10n_eg_internal_id': move.name,
                        'l10n_eg_submission_number': data.get('submissionUUID') or self.submission_uuid,
                    }}).encode(),
                })
                move.l10n_eg_eta_json_doc_id = attachment
                native.write({'attachment_id': attachment.id})
            native.write({'state': 'sent', 'error': False, 'blocking_level': False})
        elif status in ('Invalid', 'Rejected', 'Cancelled'):
            native.write({'error': _('ETA reports %s. Review ETA Submission Jobs.', status), 'blocking_level': 'error'})
            details = data.get('validationResults')
            reason = (json.dumps(details, ensure_ascii=False)[:4000] if details else
                      _('ETA reports %s. Correct the document; it will not be resent automatically.', status))
            self.last_error = reason
            move.e_invoice_issue = reason

    def _refresh(self, client):
        self.ensure_one()
        move = self.move_id
        if move.e_invoice_uuid:
            if self.submission_uuid:
                result = client.request('GET', '/api/v1.0/documentsubmissions/%s' % self.submission_uuid,
                                        params={'pageNo': 1, 'pageSize': 100})
                matches = [d for d in result.get('documentSummary', []) if d.get('uuid') == move.e_invoice_uuid]
                if len(matches) != 1:
                    raise EtaError('ETA submission does not contain the expected document.')
                data = dict(matches[0], submissionUUID=self.submission_uuid)
                if (data.get('status') in ('Invalid', 'Rejected', 'Cancelled')
                        or data.get('status') == 'Valid' and not move.l10n_eg_eta_json_doc_id):
                    data = client.request('GET', '/api/v1.0/documents/%s/raw' % move.e_invoice_uuid)
            else:
                data = client.request('GET', '/api/v1.0/documents/%s/raw' % move.e_invoice_uuid)
            self._apply_remote(data)
            return
        # A lost response is not proof of failure. Never POST again automatically.
        # Search by internal ID around the actual attempt; historical jobs stay held.
        original = json.loads(self.payload)['documents'][0] if self.payload else None
        issued = None
        if original and not self.attempted_at:
            try:
                issued = datetime.fromisoformat(original.get('dateTimeIssued', '').replace('Z', '+00:00')).replace(tzinfo=None)
            except ValueError:
                pass
        earliest = min(self.create_date, datetime.combine(move.invoice_date, datetime.min.time())) if move.invoice_date else self.create_date
        start = (self.attempted_at or issued or earliest) - timedelta(days=1)
        scan_end = fields.Datetime.now()
        stop = min(scan_end, start + timedelta(days=29))
        data = client.request('GET', '/api/v1.0/documents/search', params={
            'submissionDateFrom': start.strftime('%Y-%m-%dT%H:%M:%S'),
            'submissionDateTo': stop.strftime('%Y-%m-%dT%H:%M:%S'),
            'internalID': move.name, 'direction': 'Sent', 'pageSize': 100,
        })
        matches = [r for r in data.get('result', []) if r.get('internalId') == move.name]
        continuation = data.get('metadata', {}).get('continuationToken')
        if continuation not in (None, 'EndofResultSet'):
            self._delay('ETA returned multiple pages for this internal ID. Review is required.', seconds=900, state='unknown')
            return
        if (not matches and continuation == 'EndofResultSet' and self.first_submission_candidate
                and not self.attempted_at and stop == scan_end):
            # The original signer committed its payload before POST. A recent
            # document with neither payload nor native/custom receipt is a first
            # submission only after ETA confirms this internal ID is absent.
            self.write({'state': 'queued', 'next_attempt': False, 'last_error': False,
                        'first_submission_candidate': False, 'checked_at': fields.Datetime.now()})
            return
        if len(matches) == 1 and original:
            # Match the actual signed request, not merely a reused internal ID.
            raw = client.request('GET', '/api/v1.0/documents/%s/raw' % matches[0]['uuid'])
            document = raw.get('document')
            if isinstance(document, str):
                document = json.loads(document)
            if document == original:
                self._apply_remote(raw)
                return
        self._delay('Submission outcome needs reconciliation. No duplicate was sent.', seconds=900, state='unknown')

    def _prepare(self):
        self.ensure_one()
        move = self.move_id.with_company(self.company_id)
        if not move.invoice_date:
            raise UserError(_('Set the invoice date before ETA submission.'))
        duration = int(self.env['ir.config_parameter'].sudo().get_param(
            'egyptian_electronic_invoice.eta_expiration_duration', '7'))
        if self._effective_issue_date() < fields.Date.today() - timedelta(days=duration):
            raise UserError(_('The invoice is outside the configured ETA submission window. Review it without changing its date.'))
        if self.issue_date_policy == 'current':
            # Freeze one timestamp for JSON, canonical bytes, signing and POST.
            # A retry on another day is prepared again before any new POST.
            self.current_issue_datetime = fields.Datetime.now()
        move.action_generate_eta_json()
        document = move.action_sign_invoice()
        if not document.get('signatures') or not document['signatures'][0].get('value'):
            raise UserError(_('The signing service returned no signature.'))
        payload = json.dumps({'documents': [document]}, ensure_ascii=False, separators=(',', ':'))
        self.write({'payload': payload, 'payload_hash': hashlib.sha256(payload.encode()).hexdigest()})

    def _submit(self, client):
        self.ensure_one()
        move = self.move_id
        if move.e_invoice_uuid:
            return self._refresh(client)
        if move.e_invoice_sent:
            self.state = 'unknown'
            return self._refresh(client)
        if not self.payload:
            self._prepare()
        if self.issue_date_policy == 'current':
            expected = self._effective_issue_date()
            prepared = (fields.Datetime.context_timestamp(self.with_context(tz='Africa/Cairo'),
                        self.current_issue_datetime).date() if self.current_issue_datetime else None)
            if prepared != expected:
                self.write({'payload': False, 'payload_hash': False, 'current_issue_datetime': False})
                self._prepare()
        document = json.loads(self.payload)['documents'][0]
        try:
            issued = datetime.fromisoformat(document.get('dateTimeIssued', '').replace('Z', '+00:00'))
        except (ValueError, TypeError, AttributeError):
            issued = None
        if (not issued or not issued.tzinfo
                or issued.astimezone(pytz.timezone('Africa/Cairo')).date() != self._effective_issue_date()):
            raise UserError(_('The prepared ETA issue date differs from the approved date treatment. Review and regenerate the document before submitting.'))
        # Authenticate before the durable uncertain marker: auth failure cannot
        # have submitted a document and can be retried safely.
        client._authorize()
        check_processing_window(self.env)
        if not self._preflight_submission(client):
            return
        before = self._transmission_fingerprint()
        self.write({'state': 'unknown', 'attempted_at': fields.Datetime.now(),
                    'attempts': self.attempts + 1})
        # This method runs ONLY in the isolated worker cursor. The marker must
        # survive process death or a lost HTTP/commit response after the POST.
        self.env.cr.commit()
        # The durable marker releases the original accounting row lock. Own
        # the row again before HTTP, and retain the uncertain marker on any
        # conflict rather than send a document whose ledger changed meanwhile.
        self.env.cr.execute('SELECT id FROM account_move WHERE id=%s FOR UPDATE NOWAIT', [move.id])
        move.invalidate_recordset()
        if self._transmission_fingerprint() != before:
            raise EtaError('Posted accounting changed before ETA transmission; reconcile the retained request.')
        try:
            body = client.request('POST', '/api/v1/documentsubmissions', data=self.payload.encode())
        except EtaError as exc:
            if exc.status == 422 and exc.code == 'DuplicateSubmission':
                deadline = fields.Datetime.now() + timedelta(seconds=max(60, exc.retry_after))
                self.write({'duplicate_retry_at': deadline, 'duplicate_payload_hash': self.payload_hash,
                            'next_attempt': deadline, 'state': 'unknown',
                            'last_error': 'ETA DuplicateSubmission cooldown. The same signed request will be reconciled after Retry-After before any retry.'})
                return
            if exc.status in (401, 429):
                self._delay(exc, seconds=exc.retry_after, state='queued')
                return
            if exc.status in (400, 403, 404):
                self.write({'state': 'blocked', 'safe_to_retry': True, 'last_error': str(exc)})
                return
            raise
        accepted = [d for d in body.get('acceptedDocuments', []) if d.get('internalId') == move.name]
        rejected = [d for d in body.get('rejectedDocuments', []) if d.get('internalId') == move.name]
        if len(accepted) == 1 and accepted[0].get('uuid'):
            result = dict(accepted[0], status='Submitted', submissionUUID=body.get('submissionUUID') or body.get('submissionId'))
            self._apply_remote(result)
        elif rejected:
            error = rejected[0].get('error') or {}
            code = error.get('code') if isinstance(error, dict) else 'Rejected'
            reason = 'ETA rejected the submission (%s). %s' % (
                code, json.dumps(error, ensure_ascii=False))
            self.write({'state': 'blocked', 'safe_to_retry': True,
                        'last_error': reason[:4000]})
            move.write({'e_invoice_status': 'Error', 'eta_submission_pending': False,
                        'e_invoice_issue': reason[:4000]})
            if (self.env['ir.config_parameter'].sudo().get_param(
                    'tradeline_eta_reliability.historical_recovery_database') == self.env.cr.dbname
                    and self.issue_date_policy == 'invoice' and self._is_date_only_rejection(error)):
                self.action_approve_current_issue_date()
                self.issue_date_approval_note = 'Karim approved today’s ETA issue date for recoverable invoices while preserving the original Odoo date and accounting amounts. ETA explicitly rejected the original timestamp only (CF313), without an accepted UUID.'
                self.write({'next_attempt': fields.Datetime.now() + timedelta(minutes=10, seconds=5),
                            'last_error': 'ETA rejected the original issue time. Approved current-date preparation will run automatically after the duplicate-submission cooldown.'})
        else:
            self._delay('ETA did not return an unambiguous document result. Reconciliation is required.', state='unknown')

    @api.model
    def _is_date_only_rejection(self, node):
        if isinstance(node, str):
            try:
                node, _end = json.JSONDecoder().raw_decode(node[node.index('{'):])
            except (ValueError, TypeError):
                return False
        def leaves(value):
            if isinstance(value, list):
                return [code for child in value for code in leaves(child)]
            if isinstance(value, dict):
                children = [code for child in value.values() if isinstance(child, (dict, list)) for code in leaves(child)]
                return children or ([str(value.get('errorCode') or value.get('code'))] if value.get('errorCode') or value.get('code') else [])
            return []
        codes = set(leaves(node))
        return bool(codes) and codes == {'CF313'}

    @api.model
    def _process_id(self, job_id, client):
        """Own cursor, session lock, transaction, and failure handling per invoice."""
        with self.env.registry.cursor() as cr:
            env = api.Environment(cr, self.env.uid, dict(self.env.context))
            try:
                check_processing_window(env)
            except EtaStopped:
                return
            job = env[self._name].sudo().browse(job_id)
            job = job.with_company(job.company_id)
            cr.execute('SELECT pg_try_advisory_lock(741180, %s)', [job_id])
            if not cr.fetchone()[0]:
                return
            try:
                cr.execute('SELECT id FROM account_move WHERE id=%s FOR UPDATE NOWAIT', [job.move_id.id])
                if job.move_id.state != 'posted':
                    job.write({'state': 'blocked', 'last_error': 'Invoice is no longer posted.'})
                elif job.state == 'queued':
                    job._submit(client)
                else:
                    job._refresh(client)
                check_processing_window(env, drain_seconds=0)
                cr.commit()
                if env['ir.config_parameter'].sudo().get_param('tradeline_eta_reliability.enabled_database') == cr.dbname:
                    check_processing_window(env, drain_seconds=0)
                    env['ir.config_parameter'].sudo().set_param('tradeline_eta_reliability.last_processed_at', str(fields.Datetime.now()))
                    cr.commit()
            except EtaStopped:
                # Keep any durable pre-POST uncertain marker; do not turn a
                # planned pause into a data error or schedule more work.
                cr.rollback()
            except (psycopg2.Error, requests.RequestException, EtaError, UserError, ValueError) as exc:
                cr.rollback()
                try:
                    check_processing_window(env, drain_seconds=0)
                except EtaStopped:
                    return
                job.invalidate_recordset()
                # Rollback never removes the committed uncertain marker.
                if isinstance(exc, UserError) and job.state == 'queued':
                    # Signer outages are transient; data/age errors need correction.
                    if 'signer' in str(exc).lower() or 'signing' in str(exc).lower():
                        job._delay('The signing service is unavailable; retry scheduled.')
                    else:
                        job.write({'state': 'blocked', 'last_error': str(exc)[:500]})
                else:
                    safe = ('Database concurrency conflict; retry scheduled.' if isinstance(exc, psycopg2.Error)
                            else str(exc) if isinstance(exc, EtaError) else 'ETA communication failed; retry scheduled.')
                    job._delay(safe, seconds=getattr(exc, 'retry_after', None))
                cr.commit()
                _logger.warning('ETA job %s deferred (%s); no invoice contents logged', job_id, type(exc).__name__)
            finally:
                cr.rollback()
                cr.execute('SELECT pg_advisory_unlock(741180, %s)', [job_id])
                cr.commit()

    @api.model
    def _due_jobs(self, kind):
        states = ['queued', 'unknown'] if kind == 'submit' else ['accepted'] if kind == 'status' else ['queued', 'accepted', 'unknown', 'review']
        groups = []
        for state in states:
            domain = [('state', '=', state), '|', ('next_attempt', '=', False),
                      ('next_attempt', '<=', fields.Datetime.now())]
            if state == 'queued':
                oldest = self.sudo().search(domain, order='next_attempt ASC NULLS FIRST, id', limit=20)
                newest = self.sudo().search(domain, order='create_date desc, id desc', limit=20)
                ids = []
                for index in range(max(len(oldest), len(newest))):
                    for batch in (newest, oldest):
                        if index < len(batch) and batch[index].id not in ids:
                            ids.append(batch[index].id)
                groups.append(self.sudo().browse(ids))
            else:
                groups.append(self.sudo().search(domain, order='next_attempt ASC NULLS FIRST, id',
                                                limit=15 if state == 'unknown' else 40))
        # A historical reconciliation backlog must not starve today's submissions
        # or accepted-document validation, even when every API call is slow.
        return [group[index] for index in range(max((len(g) for g in groups), default=0))
                for group in groups if index < len(group)]

    @api.model
    def _cron_process(self, kind='all'):
        # Cron callers own their transaction. Never invoke from an invoice HTTP request.
        try:
            check_processing_window(self.env)
        except EtaStopped:
            return
        enabled_database = self.env['ir.config_parameter'].sudo().get_param(
            'tradeline_eta_reliability.enabled_database')
        if enabled_database != self.env.cr.dbname:
            # Staging database copies must never submit using production credentials.
            return
        self.env.cr.execute('SELECT pg_try_advisory_lock(741181, 1)')
        if not self.env.cr.fetchone()[0]:
            return
        clients = {}
        started = time.monotonic()
        try:
            if self.env['ir.config_parameter'].sudo().get_param(
                    'tradeline_eta_reliability.historical_recovery_database') == self.env.cr.dbname:
                self._enroll_historical(limit=500)
                self._requeue_software_holds(limit=100)
                self._requeue_changed_historical(limit=20)
            moves = self.env['account.move'].sudo().search([
                ('state', '=', 'posted'), ('move_type', 'in', ('out_invoice', 'out_refund')),
                ('eta_submission_pending', '=', True), ('e_invoice_uuid', '=', False),
                ('eta_job_ids', '=', False),
            ], limit=200, order='id')
            moves._eta_enqueue(reconcile=True)
            pending_status = self.env['account.move'].sudo().search([
                ('state', '=', 'posted'), ('move_type', 'in', ('out_invoice', 'out_refund')),
                ('e_invoice_status', '=', 'Submitted'), ('e_invoice_uuid', '!=', False),
                ('eta_job_ids', '=', False),
            ], limit=200, order='e_invoice_date, id')
            pending_status._eta_enqueue()
            legacy_recent = self.env['account.move'].sudo().search([
                ('state', '=', 'posted'), ('move_type', 'in', ('out_invoice', 'out_refund')),
                ('e_invoice_status', 'in', ('Draft', 'Error')), ('e_invoice_uuid', '=', False),
                ('invoice_date', '>=', fields.Date.today() - timedelta(days=7)),
                ('eta_job_ids', '=', False),
            ], limit=100, order='invoice_date, id')
            # Old code has no durable request/outcome ledger. Reconcile first.
            legacy_recent._eta_enqueue(reconcile=True)
            check_processing_window(self.env, drain_seconds=0)
            self.env.cr.commit()
            jobs = self._due_jobs(kind)
            for job in jobs:
                check_processing_window(self.env)
                if time.monotonic() - started > 40:
                    break
                company = job.company_id.sudo()
                if company.id not in clients:
                    clients[company.id] = EtaClient(company)
                client = clients[company.id]
                self._process_id(job.id, client)
        except EtaStopped:
            self.env.cr.rollback()
        finally:
            for client in clients.values():
                client.close()
            self.env.cr.rollback()
            self.env.cr.execute('SELECT pg_advisory_unlock(741181, 1)')
            self.env.cr.commit()

    def action_recheck(self):
        for job in self:
            if job.move_id.e_invoice_uuid:
                job.write({'state': 'accepted', 'next_attempt': False})
            elif job.state != 'unknown' and (not job.attempted_at or job.safe_to_retry):
                job.write({'state': 'queued', 'next_attempt': False, 'payload': False,
                           'attempted_at': False, 'safe_to_retry': False})
        return True

"""Proof-based recovery of a retained HTTP rejection, never a lost response.

ETA documents HTTP 422 at document submission as DuplicateSubmission, with a
ten-minute identical-payload window. A missing error code must not permanently
strand an observed HTTP rejection. New failures carry a private versioned
automatic policy; older observations still require an authenticated Send now.
Check status remains receipt-only in both cases.
https://sdk.invoicing.eta.gov.eg/einvoicingapi/01-submit-documents/
"""
import hashlib
import json
import re
from copy import deepcopy
from datetime import datetime, timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.osv import expression

from .historical_recovery import fiscal, fiscal_equal
from .client import MAX_RETRY_SECONDS
from .attachment_content import attachment_bytes
from .staff_workflow import StatusClient
from .eta_job import EtaJob
from .eta_history import HISTORY_DEFERRED

AUTO_POLICY = 1
AUTO_LIMIT = 3
AUTO_AUDIT_PREFIX = 'ETA_automatic_http422_recovery_'
HTTP422_REVIEW = 'HTTP 422 needs review: '


class EtaHttp422Recovery(models.Model):
    _inherit = 'tradeline.eta.job'

    def _submission_failure_recovery_metadata(self, exc):
        values = super()._submission_failure_recovery_metadata(exc)
        # Set only while preserving a newly observed POST failure. A legacy
        # status string, old negative search or timeout cannot acquire consent
        # to retry simply because the addon was upgraded.
        if (getattr(exc, 'status', None) == 422
                and getattr(exc, 'code', None) in (None, 'DuplicateSubmission')):
            values['automatic_http422_policy'] = AUTO_POLICY
        return values

    def _http422_automatic_count(self):
        self.ensure_one()
        return self.env['ir.attachment'].sudo().search_count([
            ('res_model', '=', self._name), ('res_id', '=', self.id),
            ('company_id', '=', self.company_id.id), ('public', '=', False),
            ('name', '=like', AUTO_AUDIT_PREFIX + '%')], limit=AUTO_LIMIT)

    @staticmethod
    def _http422_deadline(failure):
        return fields.Datetime.to_datetime(failure['observed_at']) + timedelta(seconds=failure['retry_after_seconds'])

    def _refresh(self, client):
        failure = self._retained_http422_failure()
        if (isinstance(client, StatusClient) and failure
                and type(failure.get('automatic_http422_policy')) is int
                and failure.get('automatic_http422_policy') == AUTO_POLICY):
            return self._http422_receipt_check_only(client)
        if (not isinstance(client, StatusClient) and failure
                and type(failure.get('automatic_http422_policy')) is int
                and failure.get('automatic_http422_policy') == AUTO_POLICY):
            if self._http422_automatic_count() < AUTO_LIMIT:
                return self._recover_retained_http422(client, failure, automatic=True)
            # Keep looking for a late receipt, without another POST or loss of
            # the uncertain marker. A persistent fault becomes actionable.
            deferred = self.with_context(_eta_history_deferred=HISTORY_DEFERRED)
            result = EtaJob._refresh(deferred, client)
            if self.state == 'unknown':
                deferred.write({'last_error': HTTP422_REVIEW + 'Three automatic recoveries did not resolve the rejection. '
                    'Automatic sending is paused; Accounting or IT must review the retained request and ETA receipts.',
                    'next_attempt': fields.Datetime.now() + timedelta(seconds=900)})
            self._eta_record_history()
            return result
        return super()._refresh(client)

    def _staff_check_status(self, client):
        failure = self._retained_http422_failure()
        if (failure and type(failure.get('automatic_http422_policy')) is int
                and failure.get('automatic_http422_policy') == AUTO_POLICY):
            return self._http422_receipt_check_only(client)
        return super()._staff_check_status(client)

    def _http422_receipt_check_only(self, client):
        # A successful negative status check is not a new POST failure. Keep
        # its existing cooldown/recovery due time, reason and failure count;
        # repeated staff checks must not keep moving automatic recovery away.
        before = {key: self[key] for key in ('next_attempt', 'last_error', 'failures')}
        deferred = self.with_context(_eta_history_deferred=HISTORY_DEFERRED)
        result = EtaJob._refresh(deferred, client)
        if self.state == 'unknown':
            deferred.write(before)
        self._eta_record_history()
        return result

    @staticmethod
    def _http422_address_only_change(original, current):
        """Only street/city drift for the same named domestic business ID."""
        old = original.get('receiver', {}); new = current.get('receiver', {})
        if (old.get('type') != 'B' or new.get('type') != 'B'
                or not re.fullmatch(r'[0-9]{9}', str(old.get('id') or ''))
                or any(old.get(key) != new.get(key) for key in ('id', 'name'))
                or not old.get('name')
                or old.get('address', {}).get('country') != 'EG'
                or new.get('address', {}).get('country') != 'EG'):
            return False
        candidate = deepcopy(original)
        address = candidate['receiver']['address']
        for key in ('street', 'regionCity'):
            if key in new['address']:
                address[key] = new['address'][key]
            else:
                address.pop(key, None)
        return fiscal_equal(fiscal(candidate), fiscal(current))

    def _retained_http422_failure(self):
        """Match the worker's private failure to this exact durable attempt."""
        self.ensure_one()
        move = self.move_id
        if (self.state != 'unknown' or self.safe_to_retry or not self.attempts or not self.attempted_at
                or not self.payload or not self.payload_hash
                or bool(self.duplicate_retry_at) != bool(self.duplicate_payload_hash)
                or self.duplicate_payload_hash and self.duplicate_payload_hash != self.payload_hash
                or self.submission_uuid or move.e_invoice_uuid or move.l10n_eg_uuid or move.e_invoice_sent
                or move.l10n_eg_eta_json_doc_id
                or hashlib.sha256(self.payload.encode()).hexdigest() != self.payload_hash):
            return None
        name = 'ETA_uncertain_request_failure_%s_%s_%s.json' % (
            self.attempts, self.attempted_at.strftime('%Y%m%dT%H%M%S'), self.payload_hash[:12])
        records = self.env['ir.attachment'].sudo().search([
            ('res_model', '=', self._name), ('res_id', '=', self.id),
            ('company_id', '=', self.company_id.id), ('public', '=', False), ('name', '=', name)], limit=2)
        if len(records) != 1:
            return None
        try:
            data = json.loads(attachment_bytes(records))
            expected = {'job': self.id, 'attempts': self.attempts,
                'attempted_at': fields.Datetime.to_string(self.attempted_at),
                'payload_sha256': self.payload_hash, 'database': self.env.cr.dbname,
                'company': self.company_id.id, 'operation': 'document_submission_request',
                'exception_type': 'EtaError', 'http_status': 422,
                'delivery_proven': False, 'retry_authorized': False}
            if (not isinstance(data, dict) or any(data.get(key) != value for key, value in expected.items())
                    or data.get('error_code') not in (None, 'DuplicateSubmission')):
                return None
            observed = fields.Datetime.to_datetime(data['observed_at'])
            if not self.attempted_at <= observed <= fields.Datetime.now():
                return None
            retry = data.get('retry_after_seconds', 605)
            if type(retry) is not int or not 0 <= retry <= MAX_RETRY_SECONDS:
                return None
            return dict(data, attachment_id=records.id, retry_after_seconds=max(605, retry))
        except (ValueError, TypeError, KeyError):
            return None

    def _staff_refresh_for_send(self, client):
        failure = self._retained_http422_failure()
        if not failure:
            return super()._staff_refresh_for_send(client)
        return self._recover_retained_http422(client, failure)

    def _recover_retained_http422(self, client, failure, *, automatic=False):
        deadline = self._http422_deadline(failure)
        if fields.Datetime.now() < deadline:
            self.write({'next_attempt': deadline,
                'last_error': ('ETA returned HTTP 422. Automatic receipt reconciliation will run after the duplicate-submission window.'
                    if automatic else 'ETA returned HTTP 422. Wait for the duplicate-submission window, then use Send to ETA now to reconcile and retry.')})
            return
        proof = self._history_snapshot()
        if not proof:
            self._delay('Complete taxpayer-scoped authority history is required to reconcile HTTP 422 before retrying.', seconds=300)
            return
        original = json.loads(self.payload)['documents'][0]
        issued = datetime.fromisoformat(original['dateTimeIssued'].replace('Z', '+00:00')).replace(tzinfo=None)
        now = fields.Datetime.now()
        if (not proof['from'] <= min(issued, self.attempted_at,
                datetime.combine(self.move_id.invoice_date, datetime.min.time()))
                or not proof['from'] <= proof['to'] <= now):
            self._delay('The authority-history proof does not cover this HTTP 422 request. Expand the proof before retrying.', seconds=900)
            return
        self._assert_eta_number_unique()
        self._assert_eta_document_issuer(original)
        before = self._historical_financial_fingerprint()
        if self.historical_fingerprint and self.historical_fingerprint != before:
            self._delay('Posted accounting changed after the HTTP 422 request. Reconcile the retained request before retrying.', seconds=900)
            return
        receipts = self._history_candidates(client, proof)
        if len(receipts) == 1:
            document = receipts[0].get('document')
            document = json.loads(document) if isinstance(document, str) else document
            if document == original:
                self._apply_remote(receipts[0])
                self.write({'duplicate_retry_at': False, 'duplicate_payload_hash': False})
                return
        # Corrected historical invoices can already have terminal Invalid
        # receipts. The final POST preflight approved this exact receipt set;
        # require the same proof here, rather than strand its HTTP 422 retry.
        # Any new receipt, validation change or pending/approved outcome holds.
        known_rejections = bool(receipts and self.historical_submission_verified
            and self.authority_receipt_hash == self._receipt_set_hash(receipts)
            and all(receipt.get('status') == 'Invalid' for receipt in receipts))
        if receipts and not known_rejections:
            self._delay('Other ETA receipts use this invoice number. Reconcile them before retrying the HTTP 422 request.', seconds=900)
            return
        current = self._submission_source()
        unchanged = fiscal_equal(fiscal(original), fiscal(current))
        address_only = not unchanged and self._http422_address_only_change(original, current)
        if not unchanged and not address_only:
            self._delay(HTTP422_REVIEW + 'The source changed after the request. Reconcile the retained signed request before retrying.', seconds=900)
            return
        # Archive the old marker, signed bytes and proof before releasing the
        # attempt. The ordinary submit path still GETs again and commits its
        # next durable attempt before POST. No date, amount or tax edit here.
        self._historical_archive(proof, receipts,
            '%s: observed HTTP 422 (private evidence %s), duplicate window elapsed; '
            'complete history and fresh receipt search found no receipt. %s '
            'No missing error code was inferred.' % ('Automatic recovery' if automatic else 'Manual Send now', failure['attachment_id'],
                'Prepare and sign the current street/city for the same business tax identity; preserve all fiscal values.'
                if address_only else 'Retry the retained signed payload.'))
        if address_only:
            # Generate under this same owned transaction and accounting lock.
            # A signing failure rolls back to the original durable unknown
            # request. The next submit still compares these new bytes to the
            # current source; later master-data edits cannot slip into a POST.
            self._prepare()
            prepared = json.loads(self.payload)['documents'][0]
            if not fiscal_equal(fiscal(prepared), fiscal(current)):
                raise UserError('The newly signed HTTP 422 correction differs from the verified source. Nothing was submitted.')
        if automatic:
            self.env['ir.attachment'].sudo().create({
                'name': AUTO_AUDIT_PREFIX + '%s_%s.json' % (self.attempts, self.payload_hash[:12]),
                'res_model': self._name, 'res_id': self.id, 'company_id': self.company_id.id,
                'public': False, 'mimetype': 'application/json',
                'raw': json.dumps({'database': self.env.cr.dbname, 'job': self.id,
                    'company': self.company_id.id, 'policy': AUTO_POLICY,
                    'failure_attachment': failure['attachment_id'], 'prior_attempts': self.attempts,
                    'prior_payload_sha256': failure['payload_sha256'],
                    'queued_payload_sha256': self.payload_hash, 'address_prepared': address_only,
                    'recovered_at': fields.Datetime.to_string(fields.Datetime.now())}).encode()})
        self.write({'state': 'queued', 'attempted_at': False, 'next_attempt': False,
            'last_error': False, 'historical_fingerprint': before,
            'historical_review': False, 'historical_submission_verified': True,
            'historical_reviewed_at': fields.Datetime.now(),
            'authority_receipt_hash': self._receipt_set_hash(receipts), 'first_submission_candidate': False,
            'duplicate_retry_at': False, 'duplicate_payload_hash': False})

    @api.model
    def _http422_worker_is_healthy(self, now):
        cron = self.env.ref('tradeline_eta_reliability.cron_eta_queue', raise_if_not_found=False)
        cron = cron.sudo() if cron else cron
        return bool(cron and cron.active and not cron.failure_count
            and self.env['ir.config_parameter'].sudo().get_param(
                'tradeline_eta_reliability.enabled_database') == self.env.cr.dbname
            and cron.nextcall and (cron.nextcall >= now - timedelta(minutes=3)
                or self._native_partial_progress_is_recent(now, cron)))

    @api.model
    def _attention_domain(self, now=None):
        now = now or fields.Datetime.now()
        domain = super()._attention_domain(now)
        # Only pending observed failures can be an ordinary automatic wait.
        # Keep record rules here; never sudo the job search or contact ETA.
        waiting = []
        if self._http422_worker_is_healthy(now):
            last_id = 0
            while True:
                batch = self.with_context(prefetch_fields=False).search([
                    ('id', '>', last_id), ('state', '=', 'unknown'), ('next_attempt', '>', now),
                    ('last_error', '=like', 'ETA returned HTTP 422%')], order='id', limit=100)
                if not batch:
                    break
                for job in batch:
                    failure = job._retained_http422_failure()
                    if (failure and type(failure.get('automatic_http422_policy')) is int
                            and failure['automatic_http422_policy'] == AUTO_POLICY):
                        waiting.append(job.id)
                last_id = batch[-1].id
        if waiting:
            domain = expression.AND([domain, [('id', 'not in', waiting)]])
        return expression.OR([domain,
            [('state', '=', 'unknown'), ('last_error', '=like', HTTP422_REVIEW + '%')]])

    @api.depends('state', 'last_error', 'attempts', 'attempted_at', 'payload_hash', 'next_attempt',
                 'create_date', 'move_id.e_invoice_date', 'move_id.e_invoice_issue',
                 'issue_date_policy', 'duplicate_retry_at', 'safe_to_retry', 'staff_state', 'delivery_review_proof')
    def _compute_monitoring(self):
        super()._compute_monitoring()
        for job in self:
            failure = job._retained_http422_failure()
            if (not failure or type(failure.get('automatic_http422_policy')) is not int
                    or failure.get('automatic_http422_policy') != AUTO_POLICY):
                continue
            raw = job.last_error or ''
            if raw.startswith(HTTP422_REVIEW):
                job.staff_summary = raw
                job.required_action = _('Accounting or IT must review the retained failure and receipts. Do not send another copy without reconciliation.')
                job.needs_attention = True
            else:
                job.staff_summary = _('HTTP 422 recovery is scheduled. ETA receipts will be reconciled before any retry.')
                job.required_action = _('Automatic recovery preserves the original invoice date and amounts. Check the next retry time and history; no manual resend is needed.')
                if raw and not raw.startswith('ETA returned HTTP 422'):
                    job.staff_summary = _('HTTP 422 recovery was deferred: %s', raw)
                elif raw.startswith('ETA returned HTTP 422') and job.next_attempt and job.next_attempt > fields.Datetime.now():
                    if job._http422_worker_is_healthy(fields.Datetime.now()):
                        job.needs_attention = False
            job.reason_summary = raw or job.staff_summary

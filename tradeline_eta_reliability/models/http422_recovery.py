"""Manual recovery of a retained HTTP rejection, never of a lost response.

ETA documents HTTP 422 at document submission as DuplicateSubmission, with a
ten-minute identical-payload window. A missing error code must not permanently
strand an observed HTTP rejection. Only a new, authenticated Send now request
can enter this path; cron and Check status remain receipt-only for uncertainty.
https://sdk.invoicing.eta.gov.eg/einvoicingapi/01-submit-documents/
"""
import hashlib
import json
import re
from copy import deepcopy
from datetime import datetime, timedelta

from odoo import fields, models
from odoo.exceptions import UserError

from .historical_recovery import fiscal, fiscal_equal


class EtaHttp422Recovery(models.Model):
    _inherit = 'tradeline.eta.job'

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
                or not self.payload or not self.payload_hash or self.duplicate_retry_at or self.duplicate_payload_hash
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
            data = json.loads(records.raw)
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
            if type(retry) is not int or not 0 <= retry <= 86400:
                return None
            return dict(data, attachment_id=records.id, retry_after_seconds=max(605, retry))
        except (ValueError, TypeError, KeyError):
            return None

    def _staff_refresh_for_send(self, client):
        failure = self._retained_http422_failure()
        if not failure:
            return super()._staff_refresh_for_send(client)
        deadline = self.attempted_at + timedelta(seconds=failure['retry_after_seconds'])
        if fields.Datetime.now() < deadline:
            self.write({'next_attempt': deadline,
                'last_error': 'ETA returned HTTP 422. Wait for the duplicate-submission window, then use Send to ETA now to reconcile and retry.'})
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
                return
        if receipts:
            self._delay('Other ETA receipts use this invoice number. Reconcile them before retrying the HTTP 422 request.', seconds=900)
            return
        current = self._submission_source()
        unchanged = fiscal_equal(fiscal(original), fiscal(current))
        address_only = not unchanged and self._http422_address_only_change(original, current)
        if not unchanged and not address_only:
            self._delay('The source changed after the HTTP 422 request. Reconcile the retained signed request before retrying.', seconds=900)
            return
        # Archive the old marker, signed bytes and proof before releasing the
        # attempt. The ordinary submit path still GETs again and commits its
        # next durable attempt before POST. No date, amount or tax edit here.
        self._historical_archive(proof, receipts,
            'Manual Send now: observed HTTP 422 (private evidence %s), duplicate window elapsed; '
            'complete history and fresh receipt search found no receipt. %s '
            'No missing error code was inferred.' % (failure['attachment_id'],
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
        self.write({'state': 'queued', 'attempted_at': False, 'next_attempt': False,
            'last_error': False, 'historical_fingerprint': before,
            'historical_review': False, 'historical_submission_verified': True,
            'historical_reviewed_at': fields.Datetime.now(),
            'authority_receipt_hash': self._receipt_set_hash(receipts), 'first_submission_candidate': False})

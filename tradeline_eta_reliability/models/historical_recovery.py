"""Complete historical enrollment and proof-based receipt/submission recovery."""
import ast
import hashlib
import json
import re
import sqlite3
import time
from copy import deepcopy
from contextlib import closing
from datetime import datetime, timedelta
from decimal import Decimal

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from .client import EtaError
from .eta_job import PROCESSING_REVISION

HISTORY_INDEX = '/home/odoo/data/eta-2026-authority-index.sqlite3'


def fiscal(document):
    return {k: v for k, v in document.items() if k not in ('signatures', 'dateTimeIssued')}


def fiscal_equal(a, b, path=''):
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(fiscal_equal(a[k], b[k], path + '.' + k) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(fiscal_equal(x, y, path + '[%s]' % i) for i, (x, y) in enumerate(zip(a, b)))
    monetary = bool(re.fullmatch(r'\.(netAmount|totalAmount|totalSalesAmount|totalDiscountAmount|totalItemsDiscountAmount|extraDiscountAmount)', path)
                    or re.fullmatch(r'\.taxTotals\[\d+\]\.amount', path)
                    or re.fullmatch(r'\.invoiceLines\[\d+\]\.(salesTotal|netTotal|total|taxableFees|valueDifference|itemsDiscount)', path)
                    or re.fullmatch(r'\.invoiceLines\[\d+\]\.(unitValue\.(amountEGP|amountSold)|discount\.amount|taxableItems\[\d+\]\.amount)', path))
    if monetary and type(a) in (int, float) and type(b) in (int, float):
        return abs(Decimal(str(a)) - Decimal(str(b))) < Decimal('0.005')
    return type(a) == type(b) and a == b or type(a) in (int, float) and type(b) in (int, float) and a == b


def match_document(original, current, safe_codes=(), trusted_issuer=False):
    """Permit verified master-label/address drift, never fiscal or identity drift."""
    def normalize(document, other):
        result = deepcopy(fiscal(document))
        if trusted_issuer:
            address = result.get('issuer', {}).get('address', {})
            address.pop('street', None); address.pop('regionCity', None)
        for index, line in enumerate(result.get('invoiceLines', [])):
            unit = line.get('unitValue', {})
            if unit.get('currencySold') == 'EGP':
                # Legacy Ahmed payloads included optional foreign-currency
                # zeroes; native ETA payloads omit them. Neither carries FX.
                for key in ('amountSold', 'currencyExchangeRate'):
                    value = unit.get(key)
                    if value is None or type(value) in (int, float) and value == 0:
                        unit.pop(key, None)
            others = other.get('invoiceLines', [])
            if (index < len(others) and line.get('itemCode') in safe_codes
                    and all(line.get(k) == others[index].get(k) for k in ('itemCode', 'internalCode', 'itemType'))):
                line.pop('description', None)
        buyer = result.get('receiver', {}); ref = other.get('receiver', {}); identity = str(buyer.get('id') or '')
        strong = (buyer.get('type') == 'B' and re.fullmatch(r'[0-9]{9}', identity)
                  or buyer.get('type') == 'P' and re.fullmatch(r'[0-9]{14}', identity))
        if (strong and all(buyer.get(k) == ref.get(k) for k in ('id', 'type', 'name'))
                and buyer.get('address', {}).get('country') == ref.get('address', {}).get('country') == 'EG'):
            buyer['address'] = {'country': 'EG'}
        return result
    return fiscal_equal(normalize(original, current), normalize(current, original))


def leaf_codes(node):
    if isinstance(node, list):
        return [code for item in node for code in leaf_codes(item)]
    if isinstance(node, dict):
        children = [code for value in node.values() if isinstance(value, (dict, list)) for code in leaf_codes(value)]
        code = node.get('errorCode') or node.get('code')
        return children or ([str(code)] if code else [])
    return []


def difference_paths(a, b, path=''):
    if fiscal_equal(a, b, path):
        return []
    if isinstance(a, dict) and isinstance(b, dict):
        return [p for key in sorted(set(a) | set(b)) for p in difference_paths(a.get(key), b.get(key), path + '.' + key)]
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return [p for index, (x, y) in enumerate(zip(a, b)) for p in difference_paths(x, y, path + '[%s]' % index)]
    return [path.lstrip('.')]


class PreviewRollback(Exception):
    pass


class EtaHistoricalRecovery(models.Model):
    _inherit = 'tradeline.eta.job'

    historical_review = fields.Boolean(readonly=True, index=True)
    historical_fingerprint = fields.Char(readonly=True)
    historical_reviewed_at = fields.Datetime(readonly=True, index=True)
    historical_submission_verified = fields.Boolean(readonly=True)
    historical_source_hash = fields.Char(readonly=True)

    @api.model
    def _enroll_historical(self, limit=500, move_ids=None):
        if self.env['ir.config_parameter'].sudo().get_param(
                'tradeline_eta_reliability.historical_recovery_database') != self.env.cr.dbname:
            return 0
        self.env.flush_all()
        self.env.cr.execute("""SELECT m.id,
            md5(jsonb_build_array(m.invoice_date,m.amount_total,m.amount_tax,m.amount_untaxed,m.state,m.move_type,m.company_id)::text)
            FROM account_move m JOIN res_company c ON c.id=m.company_id
            LEFT JOIN tradeline_eta_job j ON j.move_id=m.id
            WHERE m.state='posted' AND m.move_type IN ('out_invoice','out_refund')
            AND coalesce(c.client_id,'')<>'' AND coalesce(c.client_secret,'')<>''
            AND coalesce(m.e_invoice_status,'')<>'Valid' AND j.id IS NULL
            AND (%s::int[] IS NULL OR m.id=ANY(%s))
            ORDER BY m.id LIMIT %s FOR UPDATE OF m SKIP LOCKED""", [move_ids, move_ids, limit])
        rows = self.env.cr.fetchall()
        if rows:
            self.sudo().create([{'move_id': mid, 'state': 'review', 'historical_review': True,
                                 'historical_fingerprint': fingerprint} for mid, fingerprint in rows])
        self.env.cr.execute("""SELECT j.id FROM tradeline_eta_job j JOIN account_move m ON m.id=j.move_id
            JOIN res_company c ON c.id=m.company_id
            WHERE j.state IN ('unknown','blocked','invalid') AND coalesce(j.historical_fingerprint,'')=''
            AND m.state='posted' AND m.move_type IN ('out_invoice','out_refund')
            AND coalesce(c.client_id,'')<>'' AND coalesce(c.client_secret,'')<>''
            AND (%s::int[] IS NULL OR m.id=ANY(%s))
            ORDER BY j.id LIMIT 50 FOR UPDATE OF j SKIP LOCKED""", [move_ids, move_ids])
        existing = self.browse([r[0] for r in self.env.cr.fetchall()])
        for job in existing:
            job.write({'state': 'review', 'historical_review': True,
                       'historical_fingerprint': job._historical_financial_fingerprint(),
                       'historical_submission_verified': False, 'next_attempt': False})
        return len(rows) + len(existing)

    @api.model
    def _requeue_changed_historical(self, limit=20):
        self.env.flush_all()
        self.env.cr.execute("""SELECT j.id FROM tradeline_eta_job j JOIN account_move m ON m.id=j.move_id
            JOIN res_partner p ON p.id=m.partner_id WHERE j.state IN ('blocked','invalid')
            AND coalesce(j.historical_fingerprint,'')<>'' AND j.historical_reviewed_at IS NOT NULL
            AND m.state='posted' AND (date_trunc('second',m.write_date)>j.historical_reviewed_at
              OR date_trunc('second',p.write_date)>j.historical_reviewed_at
              OR EXISTS(SELECT 1 FROM account_move_line l LEFT JOIN product_product v ON v.id=l.product_id
                  LEFT JOIN product_template t ON t.id=v.product_tmpl_id WHERE l.move_id=m.id
                  AND (date_trunc('second',l.write_date)>j.historical_reviewed_at
                    OR date_trunc('second',v.write_date)>j.historical_reviewed_at
                    OR date_trunc('second',t.write_date)>j.historical_reviewed_at)))
            ORDER BY j.historical_reviewed_at,j.id LIMIT %s FOR UPDATE OF j SKIP LOCKED""", [limit])
        for job in self.browse([r[0] for r in self.env.cr.fetchall()]):
            try:
                source = job._historical_source()
                digest = hashlib.sha256(json.dumps(fiscal(source), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                fingerprint = job._historical_financial_fingerprint()
                if digest != job.historical_source_hash or fingerprint != job.historical_fingerprint:
                    self.env['ir.attachment'].sudo().create({'name': 'ETA_historical_source_refresh_%s.json' % job.id,
                        'res_model': 'account.move', 'res_id': job.move_id.id, 'company_id': job.company_id.id,
                        'mimetype': 'application/json', 'raw': json.dumps({'prior_fingerprint': job.historical_fingerprint,
                            'current_fingerprint': fingerprint, 'prior_error': job.last_error,
                            'source_last_editor': job.move_id.write_uid.id, 'at': str(fields.Datetime.now())}).encode(), 'public': False})
                    job.write({'state': 'review', 'historical_review': True, 'historical_submission_verified': False,
                               'historical_fingerprint': fingerprint, 'next_attempt': False})
                else:
                    job.historical_reviewed_at = fields.Datetime.now()
            except UserError as exc:
                job._historical_hold(exc)

    def _historical_hold(self, message, authority=False):
        self.write({'state': 'invalid' if authority else 'blocked', 'last_error': str(message)[:4000],
                    'historical_review': False, 'historical_reviewed_at': fields.Datetime.now(),
                    'historical_submission_verified': False, 'checked_at': fields.Datetime.now(), 'next_attempt': False,
                    'processing_revision': PROCESSING_REVISION, 'duplicate_retry_at': False, 'duplicate_payload_hash': False})

    @api.model
    def _requeue_software_holds(self, limit=100, move_ids=None):
        if self.env['ir.config_parameter'].sudo().get_param('tradeline_eta_reliability.historical_recovery_database') != self.env.cr.dbname:
            return 0
        domain = [('state', 'in', ['blocked', 'invalid']), ('processing_revision', '!=', PROCESSING_REVISION),
            '|', '|', '|', '|', ('last_error', '=like', 'Generated ETA net or final amount%'),
                 ('last_error', '=like', 'Foreign-currency historical recovery requires posted FX%'),
                 ('last_error', '=like', 'The invoice is outside the configured ETA submission window%'),
                 ('last_error', 'ilike', 'CF313'),
                 ('last_error', '=like', 'Existing Valid receipt differs from posted source%')]
        if move_ids is not None:
            domain.append(('move_id', 'in', move_ids))
        candidates = self.sudo().search(domain, limit=limit, order='id')
        jobs = candidates.filtered(
            lambda job: 'CF313' not in (job.last_error or '') or job._is_date_only_rejection(job.last_error))
        # A mixed genuine rejection must not occupy the first page forever.
        (candidates - jobs).write({'processing_revision': PROCESSING_REVISION})
        jobs.write({'state': 'review', 'historical_review': True, 'historical_submission_verified': False,
                    'next_attempt': False, 'processing_revision': PROCESSING_REVISION})
        return len(jobs)

    def _historical_financial_fingerprint(self):
        self.env.flush_all()
        self.env.cr.execute('SELECT md5(jsonb_build_array(invoice_date,amount_total,amount_tax,amount_untaxed,state,move_type,company_id)::text) FROM account_move WHERE id=%s', [self.move_id.id])
        return self.env.cr.fetchone()[0]

    def _history_snapshot(self):
        try:
            with closing(sqlite3.connect('file:%s?mode=ro' % HISTORY_INDEX, uri=True, timeout=1)) as db:
                row = db.execute("SELECT value FROM meta WHERE key='database'").fetchone()
                if not row or row[0] != self.env.cr.dbname:
                    raise UserError(_('The authority history proof belongs to a different database.'))
                alias = db.execute('SELECT value FROM meta WHERE key=?', ['canonical:%s' % self.company_id.id]).fetchone()
                owner = int(alias[0]) if alias else self.company_id.id
                scan = db.execute('SELECT history_from,history_to,complete,updated_at FROM scans WHERE company=?', [owner]).fetchone()
                if not scan or not scan[2]:
                    if scan and scan[3] and datetime.fromisoformat(scan[3]) < fields.Datetime.now() - timedelta(minutes=30):
                        raise EtaError('The authority-history scan has not advanced for 30 minutes. IT must restore it; no document was sent.')
                    return None
                scope = db.execute('SELECT value FROM meta WHERE key=?', ['scope:%s' % self.company_id.id]).fetchone()
                owner_scope = db.execute('SELECT value FROM meta WHERE key=?', ['scope:%s' % owner]).fetchone()
                rin = re.sub(r'\D', '', self.company_id.vat or '')
                expected = hashlib.sha256((self.company_id.config_type + '|' + rin).encode()).hexdigest()
                if not re.fullmatch(r'[0-9]{9}', rin) or not scope or scope[0] != expected or not owner_scope or owner_scope[0] != expected:
                    raise UserError(_('Authority history proof does not match this company taxpayer and ETA environment.'))
                receipts = db.execute('SELECT uuid,status FROM receipts WHERE company=? AND invoice=?',
                                      [owner, self.move_id.name]).fetchall()
                return {'from': datetime.fromisoformat(scan[0]), 'to': datetime.fromisoformat(scan[1]),
                        'receipts': {uuid: status for uuid, status in receipts}}
        except (sqlite3.Error, OSError) as exc:
            raise EtaError('The resumable authority-history index is temporarily unavailable.') from exc

    def _historical_source(self):
        move = self.move_id
        document = None
        try:
            with self.env.cr.savepoint():
                if self.issue_date_policy == 'current' and not self.current_issue_datetime:
                    self.current_issue_datetime = fields.Datetime.now()
                document = deepcopy(move._eta_correction_document())
                raise PreviewRollback()
        except PreviewRollback:
            pass
        self.invalidate_recordset(); move.invalidate_recordset()
        return document

    def _history_candidates(self, client, proof):
        # Recheck the overlapping day for delayed government indexing as well
        # as all submissions received after the frozen bulk-history snapshot.
        candidates = dict(proof['receipts']); history_start = proof['to'] - timedelta(days=1); end = fields.Datetime.now()
        if history_start > end:
            raise UserError(_('The authority-history proof has a future timestamp.'))
        while history_start < end:
            stop = min(end, history_start + timedelta(days=29)); token = None; seen = set()
            params = {'submissionDateFrom': history_start.strftime('%Y-%m-%dT%H:%M:%S'),
                      'submissionDateTo': stop.strftime('%Y-%m-%dT%H:%M:%S'),
                      'internalID': self.move_id.name, 'direction': 'Sent', 'pageSize': 100}
            while True:
                if token:
                    params['continuationToken'] = token
                time.sleep(2.1)
                data = client.request('GET', '/api/v1.0/documents/search', params=params)
                if not isinstance(data.get('result'), list):
                    raise EtaError('Incomplete authority history response; nothing was submitted.')
                for item in data['result']:
                    if item.get('internalId') == self.move_id.name and item.get('uuid'):
                        candidates[item['uuid']] = item.get('status')
                token = data.get('metadata', {}).get('continuationToken')
                if token == 'EndofResultSet':
                    break
                if not token or token in seen or len(seen) >= 20:
                    raise EtaError('Authority history pagination is incomplete; nothing was submitted.')
                seen.add(token)
            if stop == end:
                break
            history_start = stop - timedelta(seconds=1)
        known = self.move_id.e_invoice_uuid or self.move_id.l10n_eg_uuid
        if known:
            candidates.setdefault(known, None)
        if len(candidates) > 20:
            raise UserError(_('More than 20 ETA receipts use this invoice number; reconcile the repeated invoice number.'))
        raw = []; pending_uuids = list(candidates); seen_uuids = set()
        for uuid in pending_uuids:
            if uuid in seen_uuids:
                continue
            if len(seen_uuids) >= 20:
                raise UserError(_('The duplicate receipt chain exceeds 20 documents; reconcile it before submission.'))
            seen_uuids.add(uuid)
            time.sleep(.65)
            item = client.request('GET', '/api/v1.0/documents/%s/raw' % uuid)
            if item.get('uuid') != uuid or item.get('internalId') != self.move_id.name:
                raise UserError(_('An authority receipt does not match this invoice number and UUID.'))
            raw.append(item)
            validation = json.dumps(item.get('validationResults', {}))
            if 'DS301' in leaf_codes(item.get('validationResults', {})):
                references = re.findall(r'UUID\s*:\s*([0-9A-Za-z-]{20,})', validation)
                if not references:
                    raise UserError(_('ETA reports a duplicate but its original UUID could not be identified.'))
                pending_uuids.extend(ref for ref in references if ref not in seen_uuids)
        proof['verified_through'] = end
        return raw

    def _historical_matches(self, raw, current):
        document = raw.get('document')
        if isinstance(document, str):
            document = json.loads(document)
        if not isinstance(document, dict):
            return False
        move = self.move_id
        safe_codes = set()
        for line in current.get('invoiceLines', []):
            code = line.get('itemCode')
            if code and self.env['product.product'].sudo().with_context(active_test=False).search_count([('e_invoicing_code', '=', code)]) == 1:
                safe_codes.add(code)
        issuer = current.get('issuer'); trusted = False
        if move.company_id.partner_id.street and move.company_id.partner_id.city:
            for job in self.sudo().search([('company_id', '=', self.company_id.id), ('state', '=', 'valid'), ('payload', '!=', False)], order='checked_at desc', limit=1000):
                try:
                    if json.loads(job.payload)['documents'][0].get('issuer') == issuer:
                        trusted = True; break
                except (ValueError, KeyError, TypeError):
                    continue
        if not match_document(document, current, safe_codes, trusted):
            return False
        rin = re.sub(r'\D', '', move.company_id.vat or '')
        expected_net, expected_total = move._eta_posted_amounts()
        return (bool(re.fullmatch(r'[0-9]{9}', rin)) and document.get('internalID') == move.name
                and re.sub(r'\D', '', str(document.get('issuer', {}).get('id', ''))) == rin
                and abs(float(document.get('totalAmount', -1)) - expected_total) < .005
                and abs(float(document.get('netAmount', -1)) - expected_net) < .005)

    def _historical_archive(self, proof, receipts, action):
        move = self.move_id
        data = {'action': action, 'history_from': str(proof['from']), 'history_to': str(proof.get('verified_through', proof['to'])),
                'baseline_history_to': str(proof['to']),
                'checked_at': str(fields.Datetime.now()), 'financial_fingerprint': self.historical_fingerprint,
                'prior_uuid': move.e_invoice_uuid, 'prior_native_uuid': move.l10n_eg_uuid,
                'prior_status': move.e_invoice_status, 'prior_error': move.e_invoice_issue,
                'prior_json': move.e_invoice_json, 'prior_canonical': move.e_invoice_canonical,
                'prior_signature': move.static_signature, 'prior_signed': move.invoice_signed,
                'prior_sent': move.e_invoice_sent, 'prior_job_payload': self.payload,
                'prior_issue_date_policy': self.issue_date_policy, 'prior_current_issue_datetime': str(self.current_issue_datetime or ''),
                'prior_date_approved_by': self.issue_date_approved_by.id, 'prior_date_approved_at': str(self.issue_date_approved_at or ''),
                'prior_date_approval_note': self.issue_date_approval_note,
                'prior_attempted_at': str(self.attempted_at or ''), 'authority_receipts': receipts}
        self.env['ir.attachment'].sudo().create({'name': 'ETA_historical_recovery_%s.json' % self.id,
            'res_model': 'account.move', 'res_id': move.id, 'company_id': move.company_id.id,
            'mimetype': 'application/json', 'raw': json.dumps(data, ensure_ascii=False, default=str).encode(), 'public': False})

    def _review_historical(self, client):
        self.ensure_one(); move = self.move_id
        try:
            if move.state != 'posted' or move.move_type not in ('out_invoice', 'out_refund') or not move.invoice_date:
                raise UserError(_('A posted customer invoice or credit note with its original invoice date is required.'))
            expected_net, expected_total = move._eta_posted_amounts()
            if self.env['account.move'].sudo().search_count([('company_id', '=', self.company_id.id), ('name', '=', move.name), ('state', '=', 'posted'), ('move_type', 'in', ('out_invoice', 'out_refund'))]) != 1:
                raise UserError(_('The posted invoice number is not unique in this company.'))
            before = self._historical_financial_fingerprint()
            if self.historical_fingerprint and self.historical_fingerprint != before:
                raise UserError(_('Posted accounting values changed after historical enrollment; Finance must review and restart this record.'))
            if not self.historical_fingerprint:
                self.historical_fingerprint = before
            if move.l10n_eg_uuid and move.e_invoice_uuid and move.l10n_eg_uuid != move.e_invoice_uuid:
                raise UserError(_('Native and custom ETA receipts differ; reconcile both existing receipts.'))
            current = self._historical_source()
            # Net and final totals reconcile the posted signed tax difference,
            # including withholding taxes whose ETA taxTotals amounts are positive.
            if (abs(float(current['totalAmount']) - expected_total) >= .005
                    or abs(float(current['netAmount']) - expected_net) >= .005):
                raise UserError(_('Generated ETA net or final amount differs from posted accounting values. IT must correct serialization; no document was submitted.'))
            self.historical_source_hash = hashlib.sha256(json.dumps(fiscal(current), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            proof = self._history_snapshot()
            if not proof:
                selected = json.loads(self.env['ir.config_parameter'].sudo().get_param(
                    'tradeline_eta_reliability.historical_canary_move_ids', '[]'))
                if move.id in selected:
                    # A small production canary scans every interval itself;
                    # it does not claim the unfinished bulk index is complete.
                    floor = datetime(2025, 12, 1)
                    proof = {'from': floor, 'to': floor, 'receipts': {}}
            if not proof:
                self.write({'state': 'review', 'historical_review': True, 'next_attempt': fields.Datetime.now() + timedelta(minutes=5),
                            'last_error': 'Automatic authority-history scan is in progress. No new document has been sent.', 'failures': 0})
                return
            if datetime.combine(move.invoice_date, datetime.min.time()) < proof['from']:
                raise UserError(_('This invoice predates the complete authority-history proof; expand the history check.'))
            receipts = self._history_candidates(client, proof)
            valid = [r for r in receipts if r.get('status') == 'Valid']
            pending = [r for r in receipts if r.get('status') == 'Submitted']
            if len(valid) > 1:
                raise UserError(_('Multiple currently Valid ETA receipts use this invoice number; Finance must reconcile them.'))
            if valid:
                receipt = valid[0]
                if pending:
                    self.write({'state': 'review', 'next_attempt': fields.Datetime.now() + timedelta(minutes=5), 'last_error': 'An approval and another pending ETA receipt share this invoice number. Automatic status checks continue.'})
                    return
                if not self._historical_matches(receipt, current):
                    original = receipt.get('document'); original = json.loads(original) if isinstance(original, str) else original
                    paths = difference_paths(fiscal(original), fiscal(current))[:12] if isinstance(original, dict) else ['missing authority document']
                    raise UserError(_('Existing Valid receipt differs from posted source: %s. Reconcile these fields; nothing was resubmitted.', '; '.join(paths)))
                if self.env['account.move'].sudo().search_count([('id', '!=', move.id), '|', ('e_invoice_uuid', '=', receipt['uuid']), ('l10n_eg_uuid', '=', receipt['uuid'])]):
                    raise UserError(_('The existing approved ETA UUID belongs to another Odoo invoice.'))
                self._historical_archive(proof, receipts, 'Restore existing approved original; no submission')
                document = receipt['document']; document = json.loads(document) if isinstance(document, str) else document
                signature = next((s.get('value') for s in document.get('signatures', []) if s.get('signatureType') == 'I'), False)
                move.write({'e_invoice_uuid': False, 'e_invoice_long_id': False, 'e_invoice_date': False,
                            'l10n_eg_eta_json_doc_id': False, 'e_invoice_issue': False, 'e_invoice_json': repr(document),
                            'e_invoice_canonical': False, 'static_signature': signature, 'invoice_signed': bool(document.get('signatures'))})
                payload = json.dumps({'documents': [document]}, ensure_ascii=False, separators=(',', ':'))
                self.write({'payload': payload, 'payload_hash': hashlib.sha256(payload.encode()).hexdigest(), 'submission_uuid': False,
                            'historical_review': False, 'historical_reviewed_at': fields.Datetime.now(),
                            'issue_date_policy': 'invoice', 'current_issue_datetime': False,
                            'issue_date_approved_by': False, 'issue_date_approved_at': False, 'issue_date_approval_note': False})
                self._apply_remote(receipt)
                if self._historical_financial_fingerprint() != before:
                    raise EtaError('Accounting fingerprint changed during receipt restoration; transaction rolled back.')
                return
            if pending:
                if len(pending) != 1 or not self._historical_matches(pending[0], current):
                    raise UserError(_('Pending ETA receipts do not safely identify this posted invoice. Do not submit another copy.'))
                if move.e_invoice_uuid and move.e_invoice_uuid != pending[0]['uuid']:
                    self.write({'state': 'review', 'next_attempt': fields.Datetime.now() + timedelta(minutes=5), 'last_error': 'Another matching ETA receipt is pending. Automatic checks continue without resubmission.'})
                else:
                    self._apply_remote(pending[0])
                return
            if any(r.get('status') in ('Cancelled', 'Rejected') for r in receipts):
                raise UserError(_('A cancelled or buyer-rejected ETA receipt exists. Finance must trace the replacement or approve the correction route.'))
            if any(r.get('status') != 'Invalid' for r in receipts):
                raise UserError(_('An unrecognized authority receipt status prevents safe submission.'))
            for receipt in receipts:
                old = receipt.get('document'); old = json.loads(old) if isinstance(old, str) else old
                if isinstance(old, dict) and old.get('dateTimeIssued'):
                    old_issued = datetime.fromisoformat(old['dateTimeIssued'].replace('Z', '+00:00')).replace(tzinfo=None)
                    if old_issued < proof['from']:
                        raise UserError(_('An earlier authority document predates the complete history proof; expand the history before a new submission.'))
                codes = set(leaf_codes(receipt.get('validationResults', {})))
                if isinstance(old, dict) and fiscal_equal(fiscal(old), fiscal(current)) and not (codes and codes.issubset({'CF313', '4043'})):
                    self._historical_hold(json.dumps(receipt.get('validationResults', {}), ensure_ascii=False) or 'ETA rejected this unchanged invoice. Correct its source data before retrying.', authority=True)
                    return
            original = None
            if move.e_invoice_json:
                try:
                    original = ast.literal_eval(move.e_invoice_json)
                    if isinstance(original, bytes):original = ast.literal_eval(original.decode())
                except (ValueError, SyntaxError, TypeError):
                    if move.invoice_signed or move.e_invoice_sent:
                        raise UserError(_('The previous signed request is unreadable; reconcile its invoice identity before a new submission.'))
            if original and (not isinstance(original, dict) or original.get('internalID') != move.name):
                raise UserError(_('The saved ETA request uses a different or missing invoice number.'))
            if original and original.get('dateTimeIssued'):
                issued = datetime.fromisoformat(original['dateTimeIssued'].replace('Z', '+00:00')).replace(tzinfo=None)
                if issued < proof['from']:
                    raise UserError(_('The saved ETA request predates the authority-history proof.'))
            date_rejected = (any('CF313' in leaf_codes(receipt.get('validationResults', {})) for receipt in receipts)
                             or self.safe_to_retry and self._is_date_only_rejection(self.last_error))
            if not receipts and (self.attempted_at or move.e_invoice_sent) and not date_rejected:
                anchor = self.attempted_at or move.e_invoice_date or move.create_date
                if anchor and anchor > fields.Datetime.now() - timedelta(hours=24):
                    self.write({'state': 'review', 'next_attempt': anchor + timedelta(hours=24), 'last_error': 'A recent uncertain submission is in the 24-hour reconciliation window. Automatic checks continue; nothing was resent.'})
                    return
            self._historical_archive(proof, receipts, 'Queue source-complete invoice after complete authority history and fresh delta')
            move.write({'e_invoice_uuid': False, 'e_invoice_long_id': False, 'e_invoice_date': False,
                        'l10n_eg_eta_json_doc_id': False, 'e_invoice_sent': False, 'eta_submission_pending': True,
                        'e_invoice_status': 'Draft', 'e_invoice_issue': False, 'invoice_signed': False,
                        'e_invoice_json': False, 'e_invoice_canonical': False, 'static_signature': False})
            self.write({'state': 'queued', 'payload': False, 'payload_hash': False, 'attempted_at': False,
                        'submission_uuid': False, 'next_attempt': False, 'last_error': False, 'failures': 0,
                        'safe_to_retry': False, 'first_submission_candidate': False, 'historical_review': False,
                        'historical_reviewed_at': fields.Datetime.now(), 'historical_submission_verified': True})
            duration = int(self.env['ir.config_parameter'].sudo().get_param('egyptian_electronic_invoice.eta_expiration_duration', '7'))
            if date_rejected or move.invoice_date < fields.Date.today() - timedelta(days=duration):
                self.action_approve_current_issue_date()
                self.issue_date_approval_note = 'User authorized recovery of all eligible historical invoices and today’s ETA issue date while preserving the original accounting date and amounts. Complete authority history and a fresh invoice-specific delta exclude any existing approval, pending or cancelled receipt.'
            if self._historical_financial_fingerprint() != before:
                raise EtaError('Accounting fingerprint changed during historical queueing; transaction rolled back.')
        except UserError as exc:
            self._historical_hold(exc)
        except (ValueError, SyntaxError, TypeError, KeyError) as exc:
            self._historical_hold(_('Historical fiscal evidence could not be parsed (%s). IT must reconcile the retained request and authority response.', type(exc).__name__))

    def _refresh(self, client):
        if self.duplicate_retry_at and not self.move_id.e_invoice_uuid:
            return self._reconcile_duplicate_cooldown(client)
        if self.state == 'review' or self.historical_review:
            return self._review_historical(client)
        return super()._refresh(client)

    def _reconcile_duplicate_cooldown(self, client):
        """A documented 422 is distinct from a timeout; reconcile before retry."""
        self.ensure_one()
        if self.duplicate_retry_at > fields.Datetime.now():
            self.next_attempt = self.duplicate_retry_at
            return
        if (not self.payload or self.payload_hash != self.duplicate_payload_hash
                or hashlib.sha256(self.payload.encode()).hexdigest() != self.payload_hash):
            return self._historical_hold('The signed request changed after ETA DuplicateSubmission. IT must reconcile its retained payload.')
        proof = self._history_snapshot()
        if not proof:
            self._delay('Complete authority history is required before the documented duplicate-submission retry.', seconds=300)
            return
        move = self.move_id
        original = json.loads(self.payload)['documents'][0]
        issued = datetime.fromisoformat(original['dateTimeIssued'].replace('Z', '+00:00')).replace(tzinfo=None)
        if datetime.combine(move.invoice_date, datetime.min.time()) < proof['from'] or issued < proof['from']:
            return self._historical_hold('This duplicate submission predates the complete authority history; expand the proof before retrying.')
        before = self._historical_financial_fingerprint()
        if self.historical_fingerprint and self.historical_fingerprint != before:
            return self._historical_hold('Posted accounting changed after the duplicate submission. Reconcile it before retrying.')
        receipts = self._history_candidates(client, proof)
        if len(receipts) == 1:
            document = receipts[0].get('document')
            document = json.loads(document) if isinstance(document, str) else document
            if document == original:
                self._apply_remote(receipts[0])
                self.write({'duplicate_retry_at': False, 'duplicate_payload_hash': False})
                return
        if receipts:
            self._delay('Other authority receipts use this invoice number. Reconciliation continues; no duplicate was sent.', seconds=900)
            return
        source = self._historical_source()
        if not fiscal_equal(fiscal(original), fiscal(source)):
            return self._historical_hold('The source changed after ETA DuplicateSubmission. Reconcile the retained signed request before retrying.')
        if not self.historical_fingerprint:
            self.historical_fingerprint = before
        self._historical_archive(proof, receipts, 'ETA explicitly rejected DuplicateSubmission. Its Retry-After expired; complete history and a fresh delta found no receipt. Retry the retained signed request.')
        self.write({'state': 'queued', 'attempted_at': False, 'next_attempt': False, 'last_error': False,
                    'historical_fingerprint': before, 'historical_review': False,
                    'historical_submission_verified': True, 'historical_reviewed_at': fields.Datetime.now(),
                    'duplicate_retry_at': False, 'duplicate_payload_hash': False})

    def _submit(self, client):
        if (self.historical_review or self.historical_fingerprint and not self.attempted_at
                and (not self.historical_submission_verified or not self.historical_reviewed_at
                     or self.historical_reviewed_at < fields.Datetime.now() - timedelta(minutes=15))):
            self.write({'state': 'review', 'historical_review': True})
            return self._review_historical(client)
        return super()._submit(client)

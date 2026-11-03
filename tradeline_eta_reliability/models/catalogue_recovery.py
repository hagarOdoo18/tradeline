"""Recover a code rejection only from scoped ETA publication and usage proof."""
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from odoo import api, fields, models

from .client import EtaError, EtaCataloguePending
from .processing_window import check_processing_window
from .submission_safety import taxpayer
from .attachment_content import attachment_bytes

CATALOGUE_CONTINUATION = 'ETA_catalogue_continuation.json'


def active_now(record, now):
    if record.get('active') is not True:
        return False
    try:
        for key, lower in (('activeFrom', True), ('activeTo', False)):
            value = record.get(key)
            if value:
                bound = datetime.fromisoformat(value.replace('Z', '+00:00'))
                if bound.tzinfo is None:
                    bound = bound.replace(tzinfo=timezone.utc)
                if (lower and now < bound) or (not lower and now >= bound):
                    return False
        return True
    except (TypeError, ValueError):
        return False


def approved_usage(publication, usage, code, rin, now):
    return (isinstance(publication, dict) and publication.get('itemCode') == code
        and active_now(publication, now) and isinstance(usage, dict)
        and isinstance(usage.get('result'), list)
        and any(isinstance(row, dict) and row.get('itemCode') == code
                and row.get('status') == 'Approved' and active_now(row, now)
                and isinstance(row.get('requesterTaxpayer'), dict)
                and taxpayer(row['requesterTaxpayer'].get('rin')) == rin
                for row in usage.get('result', [])))


def approval_identity(proof):
    publication = proof.get('publication', {})
    usage = proof.get('usage', {})
    scope = proof.get('scope', '').split('|')
    rin = scope[1] if len(scope) == 4 else None
    keys = ('itemCode', 'codeUsageRequestID', 'codeID', 'status', 'active', 'activeFrom', 'activeTo')
    decision = {'publication': {key: publication.get(key) for key in keys},
        'usage': sorted([{key: row.get(key) for key in keys} for row in usage.get('result', [])
                         if isinstance(row, dict) and row.get('status') == 'Approved' and row.get('active') is True
                         and row.get('itemCode') == publication.get('itemCode')
                         and (rin is None or isinstance(row.get('requesterTaxpayer'), dict)
                              and taxpayer(row['requesterTaxpayer'].get('rin')) == rin)],
                        key=lambda row: str(row.get('codeUsageRequestID')))}
    return hashlib.sha256(json.dumps(decision, sort_keys=True).encode()).hexdigest()


class EtaCatalogueRecovery(models.Model):
    _inherit = 'tradeline.eta.job'

    catalogue_evidence_hash = fields.Char(readonly=True)
    catalogue_evidence_at = fields.Datetime(readonly=True)

    def _catalogue_cache_key(self, code):
        scope = self.company_id.config_type + '|' + taxpayer(self.company_id.vat) + '|EGS|' + code
        return scope, 'tradeline_eta_reliability.catalogue_proof.' + hashlib.sha256(scope.encode()).hexdigest()

    def _catalogue_read_evidence(self, key):
        # Evidence may be committed by another foreground/native worker. Read
        # the actual row instead of a process-local get_param cache entry.
        if self.env.context.get('eta_catalogue_nonblocking'):
            # Odoo uses REPEATABLE READ: the invoice's snapshot cannot see an
            # independently committed publication/approval from this step.
            with self.env.registry.cursor() as cr:
                cr.execute('SELECT value FROM ir_config_parameter WHERE key=%s', [key])
                row = cr.fetchone()
        else:
            self.env['ir.config_parameter'].sudo().flush_model(['key', 'value'])
            self.env.cr.execute('SELECT value FROM ir_config_parameter WHERE key=%s', [key])
            row = self.env.cr.fetchone()
        try:
            return json.loads(row[0]) if row else None
        except (ValueError, TypeError):
            return None

    def _catalogue_store_evidence(self, key, proof):
        # Only actual scoped GET evidence survives a deferred invoice rollback.
        # Never commit the invoice transaction or any source/payload fields.
        with self.env.registry.cursor() as cr:
            cache_env = api.Environment(cr, self.env.uid, {})
            cache_env['ir.config_parameter'].sudo().set_param(key, json.dumps(proof, ensure_ascii=False))
            cache_env.flush_all()
            cr.commit()

    def _defer_catalogue_lookup(self, pending):
        self.ensure_one()
        now = fields.Datetime.now()
        due = max(self.next_attempt or now, now + timedelta(seconds=pending.retry_after))
        values = {'next_attempt': due}
        if self.state == 'queued':
            values.update(state='review', historical_review=True, historical_submission_verified=False)
        if not self.last_error and self.state != 'unknown':
            values['last_error'] = str(pending)
        # Keep attempts/failures, all request bytes, uncertainty and cooldown
        # markers. This scheduled wait is not a new technical failure.
        self.write(values)
        if self.state != 'review':
            return
        Attachment = self.env['ir.attachment'].sudo()
        marker = Attachment.search([('res_model', '=', self._name), ('res_id', '=', self.id),
            ('company_id', '=', self.company_id.id), ('name', '=', CATALOGUE_CONTINUATION),
            ('public', '=', False)], order='id desc', limit=1)
        raw = json.dumps({'database': self.env.cr.dbname, 'job': self.id, 'company': self.company_id.id,
            'requested_at': fields.Datetime.to_string(now), 'not_before': fields.Datetime.to_string(due),
            'expires_at': fields.Datetime.to_string(now + timedelta(minutes=10)), 'phase': pending.phase}).encode()
        if marker:
            marker.raw = raw
        else:
            Attachment.create({'name': CATALOGUE_CONTINUATION, 'res_model': self._name, 'res_id': self.id,
                'company_id': self.company_id.id, 'public': False, 'mimetype': 'application/json', 'raw': raw})

    def _catalogue_continuation_candidate(self, kind):
        if kind != 'all':
            return self.browse()
        now = fields.Datetime.now()
        self.flush_model(['state', 'next_attempt', 'company_id'])
        self.env['ir.attachment'].sudo().flush_model(['res_model', 'res_id', 'company_id', 'name', 'public', 'create_date', 'write_date'])
        self.env.cr.execute("""SELECT a.id,j.id FROM ir_attachment a
            JOIN tradeline_eta_job j ON a.res_id=j.id AND a.company_id=j.company_id
            WHERE a.res_model=%s AND a.name=%s AND NOT a.public AND j.state='review'
              AND a.write_date >= %s AND (j.next_attempt IS NULL OR j.next_attempt<=%s)
            ORDER BY a.write_date,a.id LIMIT 100""", [self._name, CATALOGUE_CONTINUATION, now-timedelta(minutes=10), now])
        candidates = []
        for aid, jid in self.env.cr.fetchall():
            attachment = self.env['ir.attachment'].sudo().browse(aid)
            job = self.sudo().browse(jid)
            try:
                data = json.loads(attachment_bytes(attachment))
                requested = fields.Datetime.to_datetime(data['requested_at'])
                due = fields.Datetime.to_datetime(data['not_before'])
                expiry = fields.Datetime.to_datetime(data['expires_at'])
                if (data['database'] == self.env.cr.dbname and data['job'] == job.id
                        and data['company'] == job.company_id.id and requested <= due <= now < expiry
                        and timedelta(0) < expiry-requested <= timedelta(minutes=10)
                        and data['phase'] in ('publication', 'usage')):
                    candidates.append((data['phase'] != 'usage', aid, job))
            except (ValueError, TypeError, KeyError):
                continue
        # Finish already-started publication/usage pairs before opening more.
        return min(candidates, key=lambda item: item[:2])[2] if candidates else self.browse()

    def _catalogue_get(self, client, path, **kwargs):
        # The caller owns the existing global transport lock. Keep the
        # catalogue's stricter ten-second family limit across clients/workers.
        key = 'tradeline_eta_reliability.catalogue_next_get.' + hashlib.sha256(client.api.encode()).hexdigest()
        # Persist transport pacing independently: rolling back a failed job
        # must not erase a 429 cooldown for the next catalogue job.
        with self.env.registry.cursor() as cr:
            cr.execute('SELECT value FROM ir_config_parameter WHERE key=%s', [key])
            row = cr.fetchone()
            try:
                deadline = float(row[0]) if row else 0
            except ValueError:
                raise EtaError('ETA catalogue pacing needs IT review; no document was sent.')
        wait = max(0, deadline - time.time())
        if wait > 10.2:
            raise EtaError('ETA catalogue cooldown is active.', retry_after=min(3600, int(wait) + 1))
        if wait > 0 and self.env.context.get('eta_catalogue_nonblocking'):
            raise EtaCataloguePending(wait, 'usage' if '/codetypes/requests/' in path else 'publication')
        time.sleep(wait)
        check_processing_window(self.env)
        def save_deadline(value):
            with self.env.registry.cursor() as cr:
                rate_env = api.Environment(cr, self.env.uid, {})
                rate_env['ir.config_parameter'].sudo().set_param(key, str(value))
                rate_env.flush_all()
                cr.commit()
        save_deadline(time.time() + 10.1)
        try:
            return client.request('GET', path, **kwargs)
        except EtaError as exc:
            if exc.status in (429, 503):
                save_deadline(time.time() + max(60, exc.retry_after))
            raise

    def _catalogue_code_approved(self, client, code):
        rin = taxpayer(self.company_id.vat)
        scope, key = self._catalogue_cache_key(code)
        params = self.env['ir.config_parameter'].sudo()
        now = datetime.now(timezone.utc)
        proof = None
        try:
            saved = self._catalogue_read_evidence(key)
            if saved and saved.get('scope') == scope and type(saved.get('approved')) is bool:
                age = now - datetime.fromisoformat(saved['captured_utc'])
                ttl = timedelta(hours=6) if saved.get('approved') else timedelta(minutes=10)
                if timedelta(0) <= age < ttl:
                    proof = saved
        except (ValueError, TypeError, KeyError):
            pass
        if proof is None:
            nonblocking = self.env.context.get('eta_catalogue_nonblocking')
            partial_key = key.replace('.catalogue_proof.', '.catalogue_partial.')
            partial = self._catalogue_read_evidence(partial_key) if nonblocking else None
            publication = None
            try:
                captured = datetime.fromisoformat(partial['captured_utc'])
                if (partial['database'] == self.env.cr.dbname and partial['scope'] == scope
                        and timedelta(0) <= now-captured < timedelta(minutes=5)
                        and isinstance(partial['publication'], dict) and partial['publication']):
                    publication = partial['publication']
                    now = captured # The complete proof retains its oldest GET time.
            except (ValueError, TypeError, KeyError):
                pass
            if publication is None:
                try:
                    publication = self._catalogue_get(client, '/api/v1.0/codetypes/EGS/codes/' + quote(code, safe=''))
                except EtaError as exc:
                    if exc.status != 404:
                        raise
                    publication = {}
                if nonblocking and publication:
                    self._catalogue_store_evidence(partial_key, {'database': self.env.cr.dbname,
                        'scope': scope, 'captured_utc': now.isoformat(), 'publication': publication})
            usage = (self._catalogue_get(client, '/api/v1.0/codetypes/requests/my',
                         params={'ItemCode': code, 'Pn': 1, 'Ps': 100}) if publication else {})
            proof = {'scope': scope, 'captured_utc': now.isoformat(),
                     'publication': publication, 'usage': usage,
                     'approved': bool(approved_usage(publication, usage, code, rin, datetime.now(timezone.utc)))}
            if nonblocking:
                self._catalogue_store_evidence(key, proof)
            else:
                params.set_param(key, json.dumps(proof, ensure_ascii=False))
        approved = bool(proof.get('approved') and approved_usage(
            proof.get('publication'), proof.get('usage'), code, rin, datetime.now(timezone.utc)))
        if approved:
            self.env['ir.attachment'].sudo().create({'name': 'ETA_catalogue_approval_%s_%s.json' % (self.id, code),
                'res_model': 'account.move', 'res_id': self.move_id.id, 'company_id': self.company_id.id,
                'mimetype': 'application/json', 'raw': json.dumps(proof, ensure_ascii=False).encode(), 'public': False})
        return approved

    def _catalogue_rejection_resolved(self, client, original, current, receipt):
        # Check every current EGS code in a CV302 document, so unrelated label
        # changes cannot accidentally release the same still-invalid code.
        lines = current.get('invoiceLines', [])
        if not lines or any(str(line.get('itemType', '')).lower() != 'egs' or not line.get('itemCode') for line in lines):
            return False
        proofs = []
        for code in sorted({line['itemCode'] for line in lines}):
            if not self._catalogue_code_approved(client, code):
                return False
            _scope, key = self._catalogue_cache_key(code)
            proof = self._catalogue_read_evidence(key) or {}
            proofs.append((code, approval_identity(proof)))
        digest = hashlib.sha256(json.dumps(proofs).encode()).hexdigest()
        if self.catalogue_evidence_hash == digest and self.catalogue_evidence_at:
            try:
                received = datetime.fromisoformat(receipt['dateTimeReceived'].replace('Z', '+00:00'))
                received = received.astimezone(timezone.utc).replace(tzinfo=None)
            except (KeyError, TypeError, ValueError):
                return False
            if received >= self.catalogue_evidence_at:
                # The same entitlement already produced a newer rejection.
                # Fresh GET timestamps or product label edits cannot justify
                # another unchanged attempt; changed approval evidence can.
                return False
        self.write({'catalogue_evidence_hash': digest, 'catalogue_evidence_at': fields.Datetime.now()})
        return True

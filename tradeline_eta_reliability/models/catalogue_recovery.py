"""Recover a code rejection only from scoped ETA publication and usage proof."""
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from odoo import api, models

from .client import EtaError
from .processing_window import check_processing_window
from .submission_safety import taxpayer


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
        and any(isinstance(row, dict) and row.get('itemCode') == code
                and row.get('status') == 'Approved' and active_now(row, now)
                and taxpayer(row.get('requesterTaxpayer', {}).get('rin')) == rin
                for row in usage.get('result', [])))


class EtaCatalogueRecovery(models.Model):
    _inherit = 'tradeline.eta.job'

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
        scope = self.company_id.config_type + '|' + rin + '|EGS|' + code
        key = 'tradeline_eta_reliability.catalogue_proof.' + hashlib.sha256(scope.encode()).hexdigest()
        params = self.env['ir.config_parameter'].sudo()
        now = datetime.now(timezone.utc)
        proof = None
        try:
            saved = json.loads(params.get_param(key, 'null'))
            if saved and saved.get('scope') == scope:
                age = now - datetime.fromisoformat(saved['captured_utc'])
                ttl = timedelta(hours=6) if saved.get('approved') else timedelta(minutes=10)
                if timedelta(0) <= age < ttl:
                    proof = saved
        except (ValueError, TypeError, KeyError):
            pass
        if proof is None:
            try:
                publication = self._catalogue_get(client, '/api/v1.0/codetypes/EGS/codes/' + quote(code, safe=''))
            except EtaError as exc:
                if exc.status != 404:
                    raise
                publication = {}
            usage = (self._catalogue_get(client, '/api/v1.0/codetypes/requests/my',
                         params={'ItemCode': code, 'Pn': 1, 'Ps': 100}) if publication else {})
            proof = {'scope': scope, 'captured_utc': now.isoformat(),
                     'publication': publication, 'usage': usage,
                     'approved': bool(approved_usage(publication, usage, code, rin, now))}
            params.set_param(key, json.dumps(proof, ensure_ascii=False))
        approved = bool(proof.get('approved') and approved_usage(
            proof.get('publication'), proof.get('usage'), code, rin, now))
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
        return all(self._catalogue_code_approved(client, code) for code in sorted({line['itemCode'] for line in lines}))

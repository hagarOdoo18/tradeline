"""Short postcommit dispatch using the ordinary durable ETA job and locks."""
import json
import logging
import time
from datetime import timedelta
from functools import partial

from odoo import api, fields, models

from .client import EtaClient
from .attachment_content import attachment_bytes
from .processing_window import EtaStopped, check_processing_window

_logger = logging.getLogger(__name__)
PRIORITY_REQUEST_NAME = 'ETA_priority_dispatch_request.json'
DISPATCH_MAX_OPERATIONS = 2
DISPATCH_SECONDS = 8  # Soft budget; never interrupt an in-flight HTTP request.


def dispatch_after_commit(registry, uid, job_ids, priority_request_ids=None):
    """Never retain the posting environment or commit an invoice from its RPC."""
    try:
        with registry.cursor() as cr:
            env = api.Environment(cr, uid, {'eta_queue_cron': True})
            jobs = env['tradeline.eta.job'].sudo().browse(job_ids).exists()
            if priority_request_ids is not None:
                jobs = jobs._committed_priority_jobs(priority_request_ids)
            if jobs:
                jobs._dispatch_immediately()
    except Exception as exc:
        # The invoice is already committed. Its queue job and cron trigger are
        # the fallback; leaking an exception cannot roll back a government POST.
        _logger.warning('ETA postcommit dispatch deferred (%s); durable queue retained', type(exc).__name__)


class EtaImmediateDispatch(models.Model):
    _inherit = 'tradeline.eta.job'

    @api.model
    def _immediate_dispatch_enabled(self):
        try:
            check_processing_window(self.env)
        except EtaStopped:
            return False
        self.env['ir.config_parameter'].sudo().flush_model(['key', 'value'])
        self.env.cr.execute("SELECT value FROM ir_config_parameter WHERE key='tradeline_eta_reliability.enabled_database'")
        binding = self.env.cr.fetchone()
        cron = self.env.ref('tradeline_eta_reliability.cron_eta_queue', raise_if_not_found=False)
        return bool(binding and binding[0] == self.env.cr.dbname and cron and cron.sudo().active)

    def _schedule_immediate_dispatch(self, priority_request_ids=None):
        if self and not self.env.context.get('eta_queue_cron'):
            args = (self.env.registry, self.env.uid, tuple(self.ids))
            if priority_request_ids is not None:
                args += (tuple(priority_request_ids),)
            self.env.cr.postcommit.add(partial(dispatch_after_commit, *args))

    def _committed_priority_jobs(self, request_ids):
        # A savepoint rollback can leave a postcommit callback registered even
        # though its click was undone. Act only on the durable request records
        # captured by that callback, never merely an existing queued invoice.
        requests = self.env['ir.attachment'].sudo().browse(request_ids).exists().filtered(
            lambda a: a.res_model == self._name and a.name == PRIORITY_REQUEST_NAME)
        now = fields.Datetime.now()
        return self.filtered(lambda job: any(a.res_id == job.id
            and self._read_priority_request(a, job, now) for a in requests))

    @api.model
    def _read_priority_request(self, attachment, job, now):
        try:
            data = json.loads(attachment_bytes(attachment))
            requested = fields.Datetime.to_datetime(data['requested_at'])
            expires = fields.Datetime.to_datetime(data['expires_at'])
            return (data['database'] == self.env.cr.dbname and data['job'] == job.id
                    and data['company'] == job.company_id.id and attachment.company_id == job.company_id
                    and not attachment.public and requested <= now < expires
                    and timedelta(0) < expires - requested <= timedelta(hours=2))
        except (ValueError, TypeError, KeyError):
            return False

    def _request_priority_dispatch(self):
        """A click changes scheduling priority, never a retry or uncertainty gate."""
        if not self._immediate_dispatch_enabled():
            return False
        now = fields.Datetime.now()
        Attachment = self.env['ir.attachment'].sudo()
        requests = Attachment.browse()
        active = self.filtered(lambda job: job.state in ('queued', 'unknown', 'accepted', 'review'))
        for job in active:
            existing = Attachment.search([
                ('res_model', '=', self._name), ('res_id', '=', job.id),
                ('company_id', '=', job.company_id.id), ('name', '=', PRIORITY_REQUEST_NAME),
                ('create_date', '>=', now - timedelta(hours=2))], order='id desc', limit=20)
            valid = next((a for a in existing if self._read_priority_request(a, job, now)), None)
            if valid:
                requests |= valid
                continue
            # A later timeout/Retry-After can move next_attempt beyond ten
            # minutes. Retain the click's bounded priority across that backoff;
            # candidate selection still enforces the actual retry deadline.
            expiry = now + timedelta(hours=2)
            requests |= Attachment.create({
                'name': PRIORITY_REQUEST_NAME, 'res_model': self._name, 'res_id': job.id,
                'company_id': job.company_id.id, 'public': False, 'mimetype': 'application/json',
                'raw': json.dumps({'database': self.env.cr.dbname, 'job': job.id,
                    'company': job.company_id.id, 'requested_by': self.env.uid,
                    'requested_at': fields.Datetime.to_string(now),
                    'expires_at': fields.Datetime.to_string(expiry)}).encode()})
        if active:
            # Register the direct attempt first. Waking an idle cron before it
            # would race for the same transport and could start maintenance.
            active._schedule_immediate_dispatch(priority_request_ids=requests.ids)
            self.env.ref('tradeline_eta_reliability.cron_eta_queue').sudo()._trigger()
        return bool(active)

    @api.model
    def _priority_dispatch_candidate(self, kind):
        states = ('queued', 'unknown') if kind == 'submit' else ('accepted',) if kind == 'status' else ('queued', 'unknown', 'accepted', 'review')
        now = fields.Datetime.now()
        self.flush_model(['state', 'next_attempt', 'company_id'])
        self.env['ir.attachment'].sudo().flush_model(['res_model', 'res_id', 'company_id', 'name', 'public', 'create_date'])
        # Join before LIMIT: old completed requests cannot hide a pending click
        # outside the scheduler's ordinary oldest/newest 20-record pages.
        self.env.cr.execute("""SELECT a.id,j.id FROM ir_attachment a
            JOIN tradeline_eta_job j ON a.res_id=j.id AND a.company_id=j.company_id
            WHERE a.res_model=%s AND a.name=%s AND NOT a.public
              AND a.create_date >= %s AND j.state IN %s
              AND (j.next_attempt IS NULL OR j.next_attempt<=%s)
            ORDER BY a.id LIMIT 100""", [self._name, PRIORITY_REQUEST_NAME,
                now - timedelta(hours=2), states, now])
        for attachment_id, job_id in self.env.cr.fetchall():
            job = self.sudo().browse(job_id)
            if self._read_priority_request(self.env['ir.attachment'].sudo().browse(attachment_id), job, now):
                return job
        return self.browse()

    @api.model
    def _native_batch_candidate(self, kind, turn):
        ordinary = super()._native_batch_candidate(kind, turn)
        now = fields.Datetime.now()
        if ordinary and (ordinary.state == 'unknown'
                or ordinary.state == 'queued' and not ordinary.historical_fingerprint
                or ordinary.state == 'queued' and ordinary.historical_reviewed_at and ordinary.historical_reviewed_at >= now - timedelta(minutes=5)):
            return ordinary
        # A continuous backlog of stale status polls must not starve an
        # explicit send request. Fresh invoices, uncertain outcomes and newly
        # verified submissions above still retain their protective precedence.
        return self._priority_dispatch_candidate(kind) or ordinary

    def _dispatch_immediately(self):
        """Bounded work on committed jobs; native scheduling remains the fallback."""
        if not self._immediate_dispatch_enabled():
            return 0
        self.env.cr.execute('SELECT pg_try_advisory_lock(741181,1)')
        if not self.env.cr.fetchone()[0]:
            return 0
        clients = {}
        done = 0
        started = time.monotonic()
        try:
            for _turn in range(DISPATCH_MAX_OPERATIONS):
                # _process_id owns another cursor and may commit the pre-POST
                # unknown marker. Keep the session transport lock, but always
                # refresh this dispatch cursor's snapshot before selecting again.
                self.env.cr.rollback(); self.env.invalidate_all()
                if time.monotonic() - started >= DISPATCH_SECONDS or not self._immediate_dispatch_enabled():
                    break
                jobs = self.sudo().search([('id', 'in', self.ids)] + self._native_due_domain(), order='create_date,id')
                if not jobs:
                    break
                fresh = jobs.filtered(lambda job: job.state == 'queued' and not job.historical_fingerprint)
                job = (fresh or jobs)[:1]
                if not fresh and self.sudo().search_count(self._native_due_domain('submit') + [
                        ('state', '=', 'queued'), ('historical_fingerprint', '=', False)], limit=1):
                    # A manual old invoice never jumps ahead of a new posting.
                    break
                company = job.company_id.sudo()
                if company.id not in clients:
                    clients[company.id] = EtaClient(company)
                job_id, client = job.id, clients[company.id]
                # Do not retain selection/configuration relation locks while
                # the nested worker uses its own transaction. Keep only the
                # session-scoped global transport lock across this handoff.
                self.env.cr.rollback()
                if not self._process_id(job_id, client):
                    break
                done += 1
        finally:
            try:
                for client in clients.values():
                    client.close()
            finally:
                self.env.cr.rollback()
                self.env.cr.execute('SELECT pg_advisory_unlock(741181,1)')
                self.env.cr.commit()
        return done

"""Invoice-user operations independent of cron and backlog transport ownership.

The first RPC commits an expiring, user-bound intent. The client action executes
that intent in a second RPC. Transport uses the existing isolated worker and
per-invoice lock, but never takes the backlog's global transport lock or selects
other jobs. Neither RPC commits the user's invoice editing transaction.
"""
import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError

from .attachment_content import attachment_bytes
from odoo.osv import expression
from odoo.tools.sql import column_exists, create_column, table_exists

from .client import EtaClient, EtaError
from .eta_job import EtaJob, STATES, STAFF_SEND, STAFF_STATUS
from .processing_window import EtaStopped, check_processing_window

REQUEST_NAME = 'ETA_staff_operation.json'
STAFF_STATES = STATES + [('delivery_review', 'ETA receipt not found — delivery review')]
DELIVERY_PROOF_FIELDS = frozenset({'delivery_review_proof', 'staff_state'})
_logger = logging.getLogger(__name__)


class StatusClient(EtaClient):
    def request(self, method, path, **kwargs):
        if method != 'GET':
            raise UserError(_('Status checking cannot submit or cancel a document.'))
        return super().request(method, path, **kwargs)


class EtaStaffJob(models.Model):
    _inherit = 'tradeline.eta.job'

    def _auto_init(self):
        # A new stored compute normally schedules every retained job, which
        # would prefetch signed payloads during a busy production upgrade.
        # Existing jobs start with their exact current transport label; only
        # the few audited requests need a subsequent presentation recompute.
        if (table_exists(self.env.cr, self._table)
                and not column_exists(self.env.cr, self._table, 'staff_state')):
            create_column(self.env.cr, self._table, 'staff_state', 'varchar', 'ETA status')
            self.env.cr.execute('UPDATE tradeline_eta_job SET staff_state=state')
        return super()._auto_init()

    # This is presentation only. The transport state remains unknown and keeps
    # every retained request marker. A small stored selection permits honest
    # staff grouping without changing the worker's scheduling or retry policy.
    staff_state = fields.Selection(STAFF_STATES, string='ETA status',
        compute='_compute_staff_display_state', compute_sudo=True, store=True, index=True)
    delivery_review_proof = fields.Text(readonly=True, copy=False, groups='account.group_account_manager')

    journal_id = fields.Many2one(related='move_id.journal_id', string='Branch / sales journal', readonly=True, store=True)
    invoice_number = fields.Char(related='move_id.name', string='Invoice number', readonly=True)
    invoice_total = fields.Monetary(related='move_id.amount_total', string='Invoice total', readonly=True)
    currency_id = fields.Many2one(related='move_id.currency_id', readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su and any(DELIVERY_PROOF_FIELDS.intersection(values) for values in vals_list):
            raise AccessError(_('Delivery review findings can only be recorded by verified receipt reconciliation.'))
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.su and DELIVERY_PROOF_FIELDS.intersection(vals):
            raise AccessError(_('Delivery review findings can only be recorded by verified receipt reconciliation.'))
        return super().write(vals)

    @api.depends('state', 'payload', 'payload_hash', 'attempts', 'attempted_at',
                 'move_id', 'move_id.name', 'company_id', 'safe_to_retry',
                 'duplicate_retry_at', 'duplicate_payload_hash', 'delivery_review_proof',
                 'last_error', 'move_id.e_invoice_status', 'move_id.e_invoice_issue',
                 'move_id.e_invoice_uuid', 'move_id.l10n_eg_uuid')
    def _compute_staff_display_state(self):
        for job in self:
            job.staff_state = ('delivery_review' if job._delivery_review_details() else
                'invalid' if job._staff_has_confirmed_rejection() else job.state)

    def _staff_has_confirmed_rejection(self):
        """Presentation only: both intake refusals and final Invalid receipts.

        A refused intake has no accepted UUID, but it was still sent to ETA.
        The transport deliberately stays blocked; this label cannot authorize a
        retry or alter a receipt. Generic errors, attempts and HTTP failures do
        not prove an ETA document rejection.
        """
        self.ensure_one()
        if self.state != 'blocked':
            return False
        move = self.move_id
        if (move.e_invoice_status == 'Invalid'
                and (move.e_invoice_uuid or move.l10n_eg_uuid)
                and not (move.e_invoice_uuid and move.l10n_eg_uuid
                         and move.e_invoice_uuid != move.l10n_eg_uuid)):
            return True
        return bool(self.safe_to_retry and self.attempts and self.attempted_at
            and move.e_invoice_status == 'Error'
            and self.last_error and self.last_error.startswith('ETA rejected the submission (')
            and move.e_invoice_issue == self.last_error)

    @api.model
    def _refresh_blocked_staff_rejection_labels(self):
        """Upgrade-only, bounded presentation backfill; keep transport untouched."""
        if not self.env.su:
            raise AccessError(_('Administrative presentation refresh required.'))
        self.flush_model(['state', 'staff_state', 'safe_to_retry', 'attempts', 'attempted_at', 'move_id', 'last_error'])
        self.env['account.move'].flush_model(['e_invoice_status', 'e_invoice_issue', 'e_invoice_uuid', 'l10n_eg_uuid'])
        last_id = changed = 0
        while True:
            self.env.cr.execute("""SELECT id FROM tradeline_eta_job
                WHERE state='blocked' AND id > %s ORDER BY id LIMIT 250
                FOR UPDATE SKIP LOCKED""", [last_id])
            ids = [row[0] for row in self.env.cr.fetchall()]
            if not ids:
                break
            jobs = self.with_context(prefetch_fields=False).browse(ids)
            jobs.fetch(['state', 'staff_state', 'safe_to_retry', 'attempts', 'attempted_at', 'move_id', 'last_error'])
            jobs.mapped('move_id').fetch(['e_invoice_status', 'e_invoice_issue', 'e_invoice_uuid', 'l10n_eg_uuid'])
            for job in jobs:
                label = 'invalid' if job._staff_has_confirmed_rejection() else 'blocked'
                self.env.cr.execute("""UPDATE tradeline_eta_job SET staff_state=%s
                    WHERE id=%s AND state='blocked' AND staff_state IS DISTINCT FROM %s""",
                    [label, job.id, label])
                changed += self.env.cr.rowcount
            jobs.invalidate_recordset(['staff_state'])
            last_id = ids[-1]
        return changed

    def _delivery_review_details(self, proof=None):
        """A historical search observation, bound to this exact retained request."""
        self.ensure_one()
        job = self.sudo()
        if (job.state != 'unknown' or not job.payload or not job.payload_hash or not job.attempted_at or not job.attempts
                or job.safe_to_retry or job.duplicate_retry_at or job.duplicate_payload_hash):
            return False
        try:
            proof = json.loads(job.delivery_review_proof or '{}') if proof is None else proof
            if (not isinstance(proof, dict) or proof.get('version') != 1
                    or proof.get('source') != 'eta_documents_search'
                    or proof.get('pagination_complete') is not True
                    or type(proof.get('receipt_count')) is not int or proof['receipt_count'] != 0
                    or proof.get('receipts') != [] or proof.get('database') != self.env.cr.dbname
                    or proof.get('job') != job.id or proof.get('invoice') != job.move_id.id
                    or proof.get('company_id') != job.company_id.id or proof.get('internalId') != job.move_id.name
                    or type(proof.get('attempts')) is not int or proof['attempts'] != job.attempts
                    or proof.get('payload_sha256') != job.payload_hash
                    or hashlib.sha256(job.payload.encode()).hexdigest() != job.payload_hash):
                return False

            def utc(value):
                parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
                return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed

            attempted, start, end, checked = (utc(proof[key]) for key in
                ('attempted_at', 'search_start_utc', 'search_end_utc', 'checked_at_utc'))
            if (attempted != job.attempted_at or not start <= attempted <= end <= checked
                    or not timedelta(0) < end-start <= timedelta(days=30)):
                return False
            return {'checked': checked, 'start': start, 'end': end}
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            return False

    def _record_delivery_receipt_absence(self, proof):
        """Private, audited classification after a complete authenticated search.

        Administrative reconciliation supplies the actual search result. This
        method cannot authorize a retry, remove uncertainty or alter money.
        """
        self.ensure_one()
        if not self.env.su:
            raise AccessError(_('Verified administrative receipt reconciliation is required.'))
        self.env.cr.execute('SELECT pg_try_advisory_xact_lock(741180,%s)', [self.id])
        if not self.env.cr.fetchone()[0]:
            raise UserError(_('This invoice is being checked by ETA. Retry the review shortly.'))
        self.env.cr.execute('SELECT id FROM tradeline_eta_job WHERE id=%s FOR UPDATE NOWAIT', [self.id])
        self.invalidate_recordset()
        details = self._delivery_review_details(proof)
        now = fields.Datetime.now()
        if (not details or not timedelta(seconds=-30) <= now-details['checked'] <= timedelta(hours=1)):
            raise UserError(_('The complete ETA search proof is missing, stale or does not match the retained request. Nothing changed.'))
        encoded = json.dumps(proof, sort_keys=True, ensure_ascii=False)
        self.env['ir.attachment'].sudo().create({
            'name': 'ETA_delivery_review_%s_%s.json' % (self.id, details['checked'].strftime('%Y%m%dT%H%M%S')),
            'res_model': self._name, 'res_id': self.id, 'company_id': self.company_id.id,
            'public': False, 'mimetype': 'application/json', 'raw': encoded.encode()})
        self.write({'delivery_review_proof': encoded})
        return True

    @api.depends('state', 'last_error', 'create_date', 'attempted_at', 'next_attempt',
                 'move_id.e_invoice_date', 'move_id.e_invoice_issue', 'issue_date_policy',
                 'duplicate_retry_at', 'safe_to_retry', 'staff_state', 'delivery_review_proof')
    def _compute_monitoring(self):
        super()._compute_monitoring()
        for job in self:
            details = job._delivery_review_details()
            if not details:
                continue
            def cairo(value):
                return fields.Datetime.context_timestamp(job.with_context(tz='Africa/Cairo'), value).strftime('%d %b %Y %H:%M')
            reason = _('ETA receipt search completed %(checked)s (Cairo). No receipt was found in the checked period '
                       '%(start)s to %(end)s. Delivery of the retained request remains unconfirmed.',
                       checked=cairo(details['checked']), start=cairo(details['start']), end=cairo(details['end']))
            job.staff_summary = reason
            job.reason_summary = reason
            job.required_action = _('Accounting or IT must obtain ETA delivery confirmation for this invoice before retrying. '
                                    'Automatic receipt checks continue; no extra invoice has been sent.')

    def _apply_ir_rules(self, query, mode='read'):
        super()._apply_ir_rules(query, mode)
        if not self.env.su:
            # Use a subquery built as the actual reader, rather than a relational
            # ir.rule (those are compiled under sudo). This keeps branch and
            # custom invoice rules without enumerating the whole invoice table.
            readable = self.env['account.move']._search([])
            expression.expression([('move_id', 'in', readable)], self.sudo(), self._table, query)

    def _check_access(self, operation):
        result = super()._check_access(operation)
        if result or self.env.su or not self:
            return result
        moves = self.sudo().mapped('move_id').with_env(self.env)
        denied = set((moves - moves._filtered_access('read')).ids)
        if denied:
            forbidden = self.filtered(lambda job: job.sudo().move_id.id in denied)
            return forbidden, lambda: AccessError(_('You can only view ETA details for invoices you can access.'))
        return None

    def action_send_now(self):
        self.ensure_one()
        self.check_access('read')
        return self.move_id.action_eta_send_now()

    def action_check_status_now(self):
        self.ensure_one()
        self.check_access('read')
        return self.move_id.action_eta_check_status_now()

    def action_fix_customer(self):
        self.ensure_one()
        self.check_access('read')
        self.move_id.check_access('read')
        self.move_id.partner_id.check_access('read')
        return {'type': 'ir.actions.act_window', 'name': _('Customer details'),
                'res_model': 'res.partner', 'res_id': self.move_id.partner_id.id, 'view_mode': 'form'}

    def _staff_prepare_send(self):
        self.ensure_one()
        now = fields.Datetime.now()
        # ETA-mandated waits survive explicit clicks, including pre-upgrade jobs.
        if (self.duplicate_retry_at and self.duplicate_retry_at > now
                or self.authority_retry_at and self.authority_retry_at > now
                or self.company_id.eta_authority_retry_at and self.company_id.eta_authority_retry_at > now
                or self.next_attempt and self.next_attempt > now
                and any(word in (self.last_error or '').lower() for word in ('429', 'retry-after', 'cooldown'))):
            return False
        # An EGS code can be approved at ETA without changing the invoice's
        # itemCode. Verify that entitlement through the full receipt route;
        # the single-receipt gate requires a changed document body.
        if (self.state == 'invalid' and self.move_id.e_invoice_uuid
                and self.move_id.e_invoice_status == 'Invalid' and 'CV302' in (self.last_error or '')):
            self.historical_review = True
        if self.state != 'unknown' and (self._has_authority_duplicate_review() or self.historical_review
                or self.state in ('blocked', 'invalid') and self.historical_fingerprint
                or self.move_id.l10n_eg_uuid and self.move_id.l10n_eg_uuid != self.move_id.e_invoice_uuid):
            self.action_recheck()
        # Full-history review must run before the single-receipt correction
        # gate. That gate deliberately refuses unresolved history; calling it
        # here made every Sync click fail before the worker could reconcile.
        # Unknown attempts likewise remain GET-only even with an older UUID.
        if self.state in ('review', 'unknown'):
            self.next_attempt = False
            return True
        if self.move_id.e_invoice_uuid and self.move_id.e_invoice_status == 'Invalid':
            self.move_id._eta_queue_corrected_document(staff_operation=STAFF_SEND)
        elif self.state in ('blocked', 'invalid'):
            self.action_recheck()
        # Unknown outcomes stay GET-only. A click can bring their check forward,
        # but never clears the retained request, attempts, or receipt evidence.
        if self.state in ('queued', 'accepted', 'unknown', 'review', 'valid'):
            self.next_attempt = False
            return True
        return False
    def _staff_check_status(self, client):
        """GET-only receipt refresh; never arm a first/corrected submission."""
        self.ensure_one()
        if self._staff_has_confirmed_rejection() and not (self.move_id.e_invoice_uuid or self.move_id.l10n_eg_uuid):
            # ETA refused intake, so there is no receipt UUID to poll. Return
            # its recorded validation result without inventing uncertainty.
            return
        # A one-receipt poll cannot clear an existing complete-history hold.
        if (self._has_authority_duplicate_review()
                or self.move_id.l10n_eg_uuid and self.move_id.e_invoice_uuid
                and self.move_id.l10n_eg_uuid != self.move_id.e_invoice_uuid):
            raise UserError(_('This invoice needs complete receipt reconciliation. Its current hold is preserved. '
                              'Send to ETA now can run the full verification; it sends only when proven safe.'))
        local_history_hold = (self.state == 'blocked' and self.historical_fingerprint
            and not (self.attempts or self.attempted_at or self.move_id.e_invoice_uuid
                     or self.move_id.l10n_eg_uuid or self.move_id.e_invoice_sent))
        if self.historical_review or self.state == 'review' or local_history_hold:
            # A selected status click can GET history immediately, independently
            # of the backlog. Report the actual receipt results, but do not arm
            # a submission or clear the source/receipt matching hold.
            proof = self._history_snapshot()
            if not proof:
                raise UserError(_('Complete ETA history evidence is not available yet. This record remains in the verification queue.'))
            receipts = self._history_candidates(client, proof)
            checked = fields.Datetime.now()
            self.checked_at = checked
            local = fields.Datetime.context_timestamp(self.with_context(tz='Africa/Cairo'), checked)
            counts = {status: sum(row.get('status') == status for row in receipts)
                for status in ('Valid', 'Submitted', 'Invalid', 'Rejected', 'Cancelled')}
            client.staff_result = _('Earlier ETA receipts checked %(time)s (Cairo). Approved: %(valid)s; ETA processing: %(pending)s; '
                'rejected: %(invalid)s; cancelled: %(cancelled)s. Invoice matching remains queued. '
                'Use Sync to run guarded recovery for this invoice now.',
                time=local.strftime('%d %b %Y %H:%M:%S'), valid=counts['Valid'], pending=counts['Submitted'],
                invalid=counts['Invalid'] + counts['Rejected'], cancelled=counts['Cancelled']) if receipts else _(
                'ETA history checked %s (Cairo): no receipt was found. Use Sync to run guarded recovery for this invoice now.',
                local.strftime('%d %b %Y %H:%M:%S'))
            return
        if (self.move_id.e_invoice_uuid or self.state == 'unknown' and self.payload
                and (self.attempted_at or self.attempts)):
            # The base refresh only GETs. Historical recovery may otherwise
            # turn verified absence into a queued submission during a refresh.
            return EtaJob._refresh(self, client)
        if self.move_id.l10n_eg_uuid:
            return self._apply_remote(client.request('GET', '/api/v1.0/documents/%s/raw' % self.move_id.l10n_eg_uuid))
        raise UserError(_('No ETA receipt or unresolved transmitted request is recorded. '
                          'Use Send to ETA now to submit this invoice.'))

    def _staff_dispatch(self, mode):
        """One reserved manual lane per company; unrelated backlog keeps running."""
        self.ensure_one()
        check_processing_window(self.env)
        company_id = self.company_id.id
        self.env.cr.execute('SELECT pg_try_advisory_lock(741182,%s)', [company_id])
        if not self.env.cr.fetchone()[0]:
            return False, _('Another manual ETA operation is running for this company. Retry shortly.')
        client = None
        operations = 0
        started = time.monotonic()
        refresh_after = datetime.now(timezone.utc).isoformat()
        try:
            client = (StatusClient if mode == 'status' else EtaClient)(self.company_id.sudo())
            client.staff_result = False
            for _turn in range(3 if mode == 'send' else 1):
                job_id = self.id
                self.env.cr.rollback(); self.env.invalidate_all()
                # Release selection locks before handing off to the worker's
                # own transaction. Only the manual-lane session lock survives.
                self.env.cr.rollback()
                selected = self.with_context(eta_catalogue_manual_deadline=started + 45,
                    eta_catalogue_refresh_after=refresh_after) if mode == 'send' else self
                if not selected._process_id(job_id, client,
                        staff_operation=STAFF_SEND if mode == 'send' else STAFF_STATUS):
                    self.env.cr.rollback(); self.env.invalidate_all()
                    deadlines = [value for value in (self.duplicate_retry_at, self.authority_retry_at,
                        self.company_id.eta_authority_retry_at) if value and value > fields.Datetime.now()]
                    message = _('This invoice is already being processed, or processing was paused. '
                                'Check its ETA status before trying again. No extra copy was sent.')
                    if deadlines:
                        deadline = fields.Datetime.context_timestamp(self.with_context(tz='Africa/Cairo'), max(deadlines))
                        message = _('ETA requires waiting until %s (Cairo). Automatic recovery will continue after that time.',
                                    deadline.strftime('%d %b %Y %H:%M'))
                    return bool(operations), getattr(client, 'staff_error', False) or message
                operations += 1
                self.env.cr.rollback(); self.env.invalidate_all()
                # Continue verification -> queued -> POST for this invoice only.
                # A deferral, error, unchanged state or government receipt ends
                # the operation. Never spin on an uncertain request.
                if (mode != 'send' or self.state != 'queued'
                        or self.next_attempt and self.next_attempt > fields.Datetime.now()
                        or time.monotonic() - started >= 45):
                    break
            return bool(operations), client.staff_result
        finally:
            try:
                if client is not None:
                    client.close()
            finally:
                self.env.cr.rollback()
                self.env.cr.execute('SELECT pg_advisory_unlock(741182,%s)', [company_id])
                self.env.cr.commit()


class EtaStaffCompany(models.Model):
    _inherit = 'res.company'

    eta_authority_retry_at = fields.Datetime(string='ETA connection retry deadline', readonly=True,
        groups='base.group_system')


class EtaStaffMove(models.Model):
    _inherit = 'account.move'

    # Sales/POS readers may read invoices without access to the private job
    # model. Like standard related fields, compute only this permitted invoice's
    # summary under sudo; the invoice's own read rules still apply first.
    eta_staff_state = fields.Selection(STAFF_STATES, compute='_compute_eta_staff_status', compute_sudo=True, string='ETA status')
    eta_staff_reason = fields.Text(compute='_compute_eta_staff_status', compute_sudo=True, string='What happened')
    eta_staff_next_step = fields.Text(compute='_compute_eta_staff_status', compute_sudo=True, string='What to do')
    eta_staff_checked_at = fields.Datetime(related='eta_job_ids.checked_at', string='Last confirmed check', readonly=True)
    eta_staff_next_attempt = fields.Datetime(related='eta_job_ids.next_attempt', string='Next automatic check', readonly=True)

    @api.depends('eta_job_ids.state', 'eta_job_ids.staff_state', 'eta_job_ids.last_error', 'eta_job_ids.checked_at',
                 'eta_job_ids.next_attempt', 'e_invoice_uuid', 'e_invoice_status', 'e_invoice_sent', 'l10n_eg_uuid')
    def _compute_eta_staff_status(self):
        for move in self:
            job = move.eta_job_ids
            if job:
                move.eta_staff_state = job.staff_state
                move.eta_staff_reason = job.staff_summary
                move.eta_staff_next_step = job.required_action
            elif move.e_invoice_uuid or move.l10n_eg_uuid:
                status = move.e_invoice_status
                move.eta_staff_state = ('valid' if status == 'Valid' else 'invalid'
                    if status in ('Invalid', 'Rejected', 'Cancelled') else 'accepted')
                move.eta_staff_reason = _('An earlier ETA receipt is recorded on this invoice (%s).', status or _('received'))
                move.eta_staff_next_step = _('Use Check ETA status to refresh the existing receipt. Do not send another copy.')
            else:
                move.eta_staff_state = 'unknown' if move.e_invoice_sent else False
                move.eta_staff_reason = (_('An earlier submission is recorded without a linked receipt. Verification is required.')
                    if move.e_invoice_sent else _('No ETA tracking record is present for this invoice.'))
                move.eta_staff_next_step = _('Send to ETA now verifies earlier records before submitting when safe.')

    def _eta_staff_access(self):
        self.ensure_one()
        self.check_access('write')
        if not (self.env.is_superuser() or self.env.user.has_group('account.group_account_invoice')):
            raise AccessError(_('Invoice access is required for ETA actions.'))
        if self.state != 'posted' or self.move_type not in ('out_invoice', 'out_refund'):
            raise UserError(_('Select a posted customer invoice or credit note.'))
        if not self.company_id.sudo().client_id or not self.company_id.sudo().client_secret:
            raise UserError(_('ETA is not configured for this invoice company. Ask your accounting administrator.'))

    def _eta_staff_request(self, mode):
        self._eta_staff_access()
        had_job = bool(self.eta_job_ids)
        # Enroll just this invoice without postcommit queue dispatch/wakeups.
        if mode == 'send' or not self.eta_job_ids and (self.e_invoice_uuid or self.l10n_eg_uuid):
            self.with_context(eta_queue_cron=True)._eta_enqueue(reconcile=True)
        job = self.eta_job_ids.sudo()
        if not job:
            raise UserError(_('No ETA submission is recorded. Use Send to ETA now to submit this invoice.'))
        if not had_job and (self.e_invoice_uuid or self.l10n_eg_uuid) and self.e_invoice_status == 'Valid':
            # Enrollment for a manual check must not downgrade an approval
            # already recorded by the earlier integration if that check fails.
            job.state = 'valid'
        now = fields.Datetime.now()
        request = self.env['ir.attachment'].sudo().create({
            'name': REQUEST_NAME, 'res_model': 'tradeline.eta.job', 'res_id': job.id,
            'company_id': self.company_id.id, 'public': False, 'mimetype': 'application/json',
            'raw': json.dumps({'database': self.env.cr.dbname, 'invoice': self.id,
                'job': job.id, 'company': self.company_id.id, 'uid': self.env.uid,
                'mode': mode, 'requested_at': fields.Datetime.to_string(now),
                'expires_at': fields.Datetime.to_string(now + timedelta(minutes=5)),
                'state': 'ready'}).encode()})
        return {'type': 'ir.actions.client', 'tag': 'tradeline_eta_staff_operation',
                'params': {'invoice_id': self.id, 'request_id': request.id, 'mode': mode}}

    def action_eta_send_now(self):
        return self._eta_staff_request('send')

    def action_eta_check_status_now(self):
        return self._eta_staff_request('status')

    def action_eta_status_details(self):
        self.ensure_one()
        self.check_access('read')
        return {'type': 'ir.actions.act_window', 'name': _('ETA status'),
                'res_model': 'tradeline.eta.job', 'view_mode': 'list,form',
                'domain': [('move_id', '=', self.id)]}

    def action_eta_execute_staff_request(self, request_id):
        """Second RPC. No changes to the caller's transaction or invoice edits."""
        self._eta_staff_access()
        invoice_id, uid, context = self.id, self.env.uid, dict(self.env.context)
        # Keep all transport and claims on owned cursors. The immutable saved
        # intent must exist: rolled-back clicks and cross-user replays fail.
        with self.env.registry.cursor() as cr:
            env = api.Environment(cr, uid, context)
            move = env['account.move'].browse(invoice_id)
            move._eta_staff_access()
            cr.execute('SELECT pg_try_advisory_xact_lock(741183,%s)', [request_id])
            if not cr.fetchone()[0]:
                raise UserError(_('This request is already running. Refresh the invoice status.'))
            request = env['ir.attachment'].sudo().browse(request_id).exists()
            try:
                data = json.loads(attachment_bytes(request)) if request else {}
                now = fields.Datetime.now()
                valid = (request and request.name == REQUEST_NAME and not request.public
                    and request.res_model == 'tradeline.eta.job'
                    and request.company_id == move.company_id
                    and data['database'] == cr.dbname and data['uid'] == uid
                    and data['invoice'] == move.id and data['company'] == move.company_id.id
                    and data['job'] == request.res_id and data['mode'] in ('send', 'status')
                    and fields.Datetime.to_datetime(data['requested_at']) <= now
                    < fields.Datetime.to_datetime(data['expires_at'])
                    <= fields.Datetime.to_datetime(data['requested_at']) + timedelta(minutes=5))
            except (ValueError, KeyError, TypeError):
                valid = False
            if not valid:
                raise AccessError(_('This ETA request is missing, expired or belongs to another user or company.'))
            if data['state'] != 'ready':
                return {'title': _('ETA operation already started'), 'message': _(
                    'Refresh the invoice to see its result. This click was not submitted again.'), 'type': 'info'}
            job = env['tradeline.eta.job'].sudo().browse(data['job']).exists()
            if not job or job.move_id != move:
                raise AccessError(_('The ETA request does not match this invoice.'))
            cr.execute("SELECT value FROM ir_config_parameter WHERE key='tradeline_eta_reliability.enabled_database'")
            binding = cr.fetchone()
            if not binding or binding[0] != cr.dbname:
                raise UserError(_('ETA transport is not enabled for this database. Nothing was sent.'))
            try:
                check_processing_window(env)
            except EtaStopped as exc:
                raise UserError(_('ETA processing is paused by the owner. Nothing was sent.')) from exc
            data['state'] = 'started'
            request.raw = json.dumps(data).encode()
            cr.commit()
            try:
                done, message = job._staff_dispatch(data['mode'])
            except EtaStopped:
                done, message = False, _('ETA processing was paused by the owner. Nothing further was sent.')
            except EtaError as exc:
                done, message = False, str(exc)
            cr.rollback(); env.invalidate_all()
            result = {'title': dict(job._fields['state']._description_selection(env)).get(job.state),
                      'message': message or job.staff_summary, 'type': 'success' if job.state == 'valid'
                      else 'warning' if not done or job.state in ('blocked', 'invalid', 'unknown') else 'info'}
            data.update(state='finished', finished_at=fields.Datetime.to_string(fields.Datetime.now()), result=result)
            request.raw = json.dumps(data).encode()
            cr.commit()
            # Keep ETA operation results in the private request audit, not the
            # invoice's shared chatter/bus where branch followers can see them.
            # The requesting accountant receives the result directly in the UI.
            return result

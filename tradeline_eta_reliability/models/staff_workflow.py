"""Invoice-user operations independent of cron and backlog transport ownership.

The first RPC commits an expiring, user-bound intent. The client action executes
that intent in a second RPC. Transport uses the existing isolated worker and
per-invoice lock, but never takes the backlog's global transport lock or selects
other jobs. Neither RPC commits the user's invoice editing transaction.
"""
import json
import logging
import time
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError

from .client import EtaClient, EtaError
from .eta_job import EtaJob, STAFF_SEND, STAFF_STATUS
from .processing_window import EtaStopped, check_processing_window

REQUEST_NAME = 'ETA_staff_operation.json'
_logger = logging.getLogger(__name__)


class StatusClient(EtaClient):
    def request(self, method, path, **kwargs):
        if method != 'GET':
            raise UserError(_('Status checking cannot submit or cancel a document.'))
        return super().request(method, path, **kwargs)


class EtaStaffJob(models.Model):
    _inherit = 'tradeline.eta.job'

    journal_id = fields.Many2one(related='move_id.journal_id', string='Branch / sales journal', readonly=True, store=True)
    invoice_number = fields.Char(related='move_id.name', string='Invoice number', readonly=True)
    invoice_total = fields.Monetary(related='move_id.amount_total', string='Invoice total', readonly=True)
    currency_id = fields.Many2one(related='move_id.currency_id', readonly=True)

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
        if self.state != 'unknown' and (self._has_authority_duplicate_review() or self.historical_review
                or self.move_id.l10n_eg_uuid and self.move_id.l10n_eg_uuid != self.move_id.e_invoice_uuid):
            self.action_recheck()
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
        # A one-receipt poll cannot clear an existing complete-history hold.
        if self._has_authority_duplicate_review() or self.historical_review or self.state == 'review':
            raise UserError(_('This invoice needs complete receipt reconciliation. Its current hold is preserved. '
                              'Send to ETA now can run the full verification; it sends only when proven safe.'))
        if (self.move_id.e_invoice_uuid or self.state == 'unknown' and self.payload
                and (self.attempted_at or self.attempts)):
            # The base refresh only GETs. Historical recovery may otherwise
            # turn verified absence into a queued submission during a refresh.
            return EtaJob._refresh(self, client)
        raise UserError(_('No ETA receipt or unresolved transmitted request is recorded. '
                          'Use Send to ETA now to submit this invoice.'))

    def _staff_dispatch(self, mode):
        """One reserved manual lane per company; unrelated backlog keeps running."""
        self.ensure_one()
        check_processing_window(self.env)
        self.env.cr.execute('SELECT pg_try_advisory_lock(741182,%s)', [self.company_id.id])
        if not self.env.cr.fetchone()[0]:
            return False, _('Another manual ETA operation is running for this company. Retry shortly.')
        client = (StatusClient if mode == 'status' else EtaClient)(self.company_id.sudo())
        operations = 0
        started = time.monotonic()
        try:
            for _turn in range(3 if mode == 'send' else 1):
                job_id = self.id
                self.env.cr.rollback(); self.env.invalidate_all()
                # Release selection locks before handing off to the worker's
                # own transaction. Only the manual-lane session lock survives.
                self.env.cr.rollback()
                if not self._process_id(job_id, client,
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
            return bool(operations), False
        finally:
            client.close()
            self.env.cr.rollback()
            self.env.cr.execute('SELECT pg_advisory_unlock(741182,%s)', [client.company.id])
            self.env.cr.commit()


class EtaStaffCompany(models.Model):
    _inherit = 'res.company'

    eta_authority_retry_at = fields.Datetime(string='ETA connection retry deadline', readonly=True,
        groups='base.group_system')


class EtaStaffMove(models.Model):
    _inherit = 'account.move'

    eta_staff_state = fields.Selection(related='eta_job_ids.state', string='ETA status', readonly=True)
    eta_staff_reason = fields.Text(related='eta_job_ids.staff_summary', string='What happened', readonly=True)
    eta_staff_next_step = fields.Text(related='eta_job_ids.required_action', string='What to do', readonly=True)
    eta_staff_checked_at = fields.Datetime(related='eta_job_ids.checked_at', string='Last confirmed check', readonly=True)
    eta_staff_next_attempt = fields.Datetime(related='eta_job_ids.next_attempt', string='Next automatic check', readonly=True)

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
        # Enroll just this invoice without postcommit queue dispatch/wakeups.
        if mode == 'send':
            self.with_context(eta_queue_cron=True)._eta_enqueue(reconcile=True)
        job = self.eta_job_ids.sudo()
        if not job:
            raise UserError(_('No ETA submission is recorded. Use Send to ETA now to submit this invoice.'))
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
                data = json.loads(request.raw) if request else {}
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
            done, message = job._staff_dispatch(data['mode'])
            cr.rollback(); env.invalidate_all()
            result = {'title': dict(job._fields['state']._description_selection(env)).get(job.state),
                      'message': message or job.staff_summary, 'type': 'success' if job.state == 'valid'
                      else 'warning' if not done or job.state in ('blocked', 'invalid', 'unknown') else 'info'}
            data.update(state='finished', finished_at=fields.Datetime.to_string(fields.Datetime.now()), result=result)
            request.raw = json.dumps(data).encode()
            cr.commit()
            # The receipt and audit result must not depend on staff having an
            # outgoing email address or permission to notify invoice followers.
            try:
                with cr.savepoint():
                    move.with_context(mail_notify_force_send=False, mail_post_autofollow=False).message_post(
                        body=_('ETA %(operation)s requested by %(user)s: %(status)s. %(detail)s',
                            operation=_('direct send') if data['mode'] == 'send' else _('status check'),
                            user=env.user.display_name, status=result['title'], detail=result['message']),
                        subtype_xmlid='mail.mt_note', author_id=env.user.partner_id.id,
                        email_from=env.user.email or 'odoo@localhost')
            except (AccessError, UserError):
                _logger.info('ETA staff result retained in private audit; invoice note unavailable')
            cr.commit()
            return result

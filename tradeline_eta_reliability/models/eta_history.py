"""Durable, internal invoice notes for meaningful ETA events, without mail."""
import hashlib
import json
from datetime import datetime, timezone

from markupsafe import Markup

from odoo import api, fields, models, _

from .eta_monitoring import error_details


EVENT_FIELDS = frozenset({'state', 'last_error', 'attempts', 'checked_at',
    'submission_uuid', 'issue_date_policy', 'issue_date_approved_at', 'delivery_review_proof'})
HISTORY_DEFERRED = object()


class EtaHistoryJob(models.Model):
    _inherit = 'tradeline.eta.job'

    history_event_key = fields.Char(readonly=True, copy=False)
    eta_history_ids = fields.Many2many('mail.message', compute='_compute_eta_history', string='ETA history')

    @api.depends('history_event_key', 'move_id.message_ids')
    def _compute_eta_history(self):
        # Like the chatter reader, first verify the source invoice access, then
        # fetch only its notes. mail.message search otherwise hides non-followed
        # threads even from accounting staff who can read their invoices.
        self.move_id.check_access('read')
        notes = self.env['mail.message'].sudo().search([
            ('model', '=', 'account.move'), ('res_id', 'in', self.move_id.ids),
            ('subtype_id', '=', self.env.ref('tradeline_eta_reliability.mt_eta_history').id)],
            order='date desc, id desc')
        for job in self:
            job.eta_history_ids = notes.filtered(lambda note: note.res_id == job.move_id.id)

    def _eta_history_key(self):
        self.ensure_one()
        # Poll counters, check times and shifting retry deadlines are not events.
        values = [self.state, self.last_error or '', self.attempts, self.submission_uuid or '',
            self.issue_date_policy, str(self.issue_date_approved_at or ''), self.delivery_review_proof or '']
        return hashlib.sha256(json.dumps(values).encode()).hexdigest()

    def _eta_history_note(self, title, lines):
        self.ensure_one()
        body = Markup('<p><strong>%s</strong></p>') % title
        body += Markup('').join(Markup('<p>%s</p>') % line for line in lines if line)
        # Direct creation deliberately avoids mail.thread's follower notification
        # pipeline. This internal subtype is also hidden from portal customers.
        return self.env['mail.message'].sudo().create({
            'model': 'account.move', 'res_id': self.move_id.id, 'body': body,
            'message_type': 'comment', 'subtype_id': self.env.ref(
                'tradeline_eta_reliability.mt_eta_history').id,
            'author_id': self.env.user.partner_id.id, 'partner_ids': [],
        })

    def _eta_record_history(self, *, previous=False, started=False, approved=False):
        self.ensure_one()
        key = self._eta_history_key()
        if self.history_event_key == key:
            return
        detail, codes = error_details(self.last_error or '')
        title = dict(self._fields['state']._description_selection(self.env))[self.state]
        lines = [self.staff_summary, _('Next step: %s', self.required_action)]
        if started:
            title = _('Submission attempt started')
            lines = [_('Attempt %s started. ETA receipt and approval are not confirmed yet.', self.attempts),
                     _('Check ETA status before considering another copy.')]
            # A retained error belongs to the previous attempt, not this one.
            detail = ''; codes = set()
        elif approved:
            title = _('Current ETA issue date approved')
            lines = [_('The original Odoo invoice date and accounting amounts are preserved.'),
                     _('This approval does not confirm submission or ETA acceptance.')]
        if 'CF313' in codes:
            lines.append(_('CF313: ETA rejected the issue date/time because it is outside its allowed submission window.'))
            try:
                document = json.loads(self.payload or '{}').get('documents', [{}])[0]
                issued = datetime.fromisoformat(document.get('dateTimeIssued', '').replace('Z', '+00:00'))
                if issued.tzinfo:
                    issued = issued.astimezone(timezone.utc).replace(tzinfo=None)
                    local = fields.Datetime.context_timestamp(self.with_context(tz='Africa/Cairo'), issued)
                    lines.append(_('Issue time in the rejected document: %s (Cairo).', local.strftime('%d %b %Y %H:%M:%S')))
            except (ValueError, TypeError, AttributeError, IndexError):
                pass
            if self.attempted_at:
                local = fields.Datetime.context_timestamp(self.with_context(tz='Africa/Cairo'), self.attempted_at)
                lines.append(_('Submission attempt time: %s (Cairo).', local.strftime('%d %b %Y %H:%M:%S')))
        if detail:
            lines.append(_('Recorded message: %s', detail[:1600]))
        if self.next_attempt and self.next_attempt > fields.Datetime.now() and not (started or approved):
            # Explicit Cairo label makes this immutable note unambiguous for
            # readers whose browser/user timezone differs from the company.
            when = fields.Datetime.context_timestamp(self.with_context(tz='Africa/Cairo'), self.next_attempt)
            lines.append(_('Next scheduled check / retry: %s (Cairo).', when.strftime('%d %b %Y %H:%M:%S')))
        if previous:
            title = _('Previous recorded ETA status: %s', title)
            lines.insert(0, _('Status retained before this update. Earlier events were not logged in this history.'))
            prior = self.move_id.e_invoice_issue or ''
            if prior and prior != self.last_error:
                prior_detail, prior_codes = error_details(prior)
                lines.append(_('Earlier invoice message (may belong to an older attempt): %s', prior_detail[:1600]))
                if 'CF313' in prior_codes:
                    lines.append(_('That earlier CF313 rejection concerns the issue date/time, not VAT.'))
        if self.move_id.e_invoice_uuid:
            lines.append(_('ETA receipt reference: %s', self.move_id.e_invoice_uuid))
        self._eta_history_note(_('ETA — %s', title), lines)
        super(EtaHistoryJob, self).write({'history_event_key': key})

    @api.model_create_multi
    def create(self, vals_list):
        jobs = super().create(vals_list)
        for job in jobs:
            job._eta_record_history()
        return jobs

    def write(self, vals):
        if self.env.context.get('_eta_history_deferred') is HISTORY_DEFERRED or not EVENT_FIELDS.intersection(vals):
            return super().write(vals)
        before = {job.id: (job.attempts, job.issue_date_approved_at) for job in self}
        for job in self:
            if not job.history_event_key:
                job._eta_record_history(previous=True)
        result = super().write(vals)
        for job in self:
            attempts, approval = before[job.id]
            job._eta_record_history(started=job.attempts > attempts,
                approved=bool(job.issue_date_approved_at and job.issue_date_approved_at != approval))
        return result

    def _apply_remote(self, data):
        # Applying a rejected receipt writes its status and details separately.
        # Record the final result once, with its actual reason attached.
        for job in self:
            if not job.history_event_key:
                job._eta_record_history(previous=True)
        deferred = self.with_context(_eta_history_deferred=HISTORY_DEFERRED)
        result = super(EtaHistoryJob, deferred)._apply_remote(data)
        for job in self:
            job._eta_record_history()
        return result


class EtaHistoryMove(models.Model):
    _inherit = 'account.move'

    def _eta_approve_vat_bypass(self, reason):
        result = super()._eta_approve_vat_bypass(reason)
        for move in self:
            if move.eta_job_ids:
                move.eta_job_ids._eta_history_note(_('ETA — Sending without VAT approved'), [
                    _('Reason: %s', move.eta_vat_bypass_reason),
                    _('Approval recorded. ETA submission and acceptance are not confirmed by this approval. Other ETA checks still apply.')])
        return result

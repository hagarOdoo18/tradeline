from datetime import datetime, time, timedelta

import pytz

from odoo import api, fields, models
from odoo.osv import expression
from odoo.addons.tradeline_operations_monitor.models.operations_monitor import signal
from .processing_window import EtaStopped, check_processing_window

DAILY_ALERT_START = 'tradeline_eta_reliability.daily_alert_start_date'
LEGACY_BACKLOG_CODES = {'eta:stalled', 'eta:rejected', 'eta:progress'}


class OperationsMonitor(models.Model):
    _inherit = 'tradeline.operations.monitor'

    eta_attention_count = fields.Integer(
        string='ETA invoices needing attention', compute='_compute_eta_attention_count',
        help='Invoices visible in your selected companies. Open the log to see every invoice and its reason.')

    def _compute_eta_attention_count(self):
        jobs = self.env['tradeline.eta.job']
        count = jobs.search_count(jobs._attention_domain())
        for monitor in self:
            monitor.eta_attention_count = count

    def action_eta_invoice_log(self):
        self._ensure_admin()
        return self.env['ir.actions.actions']._for_xml_id('tradeline_eta_reliability.action_eta_invoice_log')

    def _reconcile(self, observations, completed, now):
        # ETA observations belong to the cron probe. Resolve them only when
        # that probe completed successfully, including existing eta:* incidents.
        if 'crons' in completed:
            completed = set(completed) | {'eta'}
        notices = super()._reconcile(observations, completed, now)
        # Retire the old all-history aggregates silently. Their disappearance
        # means the notification scope changed, not that the backlog recovered.
        for item in notices.filtered(lambda item: item.code in LEGACY_BACKLOG_CODES and item.state == 'resolved'):
            item.details = ('Historical ETA alert retired when daily-only monitoring began. '
                            'This does not mean the backlog was cleared. Last observation: %s' % (item.details or ''))
        return notices.filtered(lambda item: item.code not in LEGACY_BACKLOG_CODES)

    def _eta_daily_alert_domain(self, now):
        """A fixed Cairo rollout date; unresolved daily invoices never age out.

        Use invoice origin, not job timestamps, retries, current ETA issue date
        or historical flags: recovery may update all of those for old invoices.
        It may also add historical flags to genuinely new daily failures.
        """
        params = self.env['ir.config_parameter'].sudo()
        start = params.get_param(DAILY_ALERT_START)
        if not start:
            start = fields.Date.to_string(fields.Datetime.context_timestamp(
                self.with_context(tz='Africa/Cairo'), now).date())
            params.set_param(DAILY_ALERT_START, start)
        day = fields.Date.to_date(start)
        midnight = pytz.timezone('Africa/Cairo').localize(datetime.combine(day, time.min))
        utc_start = midnight.astimezone(pytz.UTC).replace(tzinfo=None)
        return [('move_id.create_date', '>=', utc_start), ('move_id.invoice_date', '>=', day)]

    def _probe_crons(self, now):
        observations = super()._probe_crons(now)
        cron = self.env.ref('tradeline_eta_reliability.cron_eta_queue', raise_if_not_found=False)
        jobs = self.env['tradeline.eta.job'].sudo()
        daily = self._eta_daily_alert_domain(now)
        native_progress = jobs._native_partial_progress_is_recent(now, cron)
        enabled_database = self.env['ir.config_parameter'].sudo().get_param('tradeline_eta_reliability.enabled_database')
        paused = False
        try:
            check_processing_window(self.env, now=now)
        except EtaStopped as exc:
            paused = True
            if not exc.planned:
                observations.append(signal('eta:cutoff', 'ETA cutoff configuration needs review',
                    'ETA processing stopped because its owner cutoff is missing or invalid.',
                    'Review the database-bound owner drain and cutoff timestamps before resuming.', 'critical'))
        if not paused and (not cron or not cron.active or cron.failure_count or enabled_database != self.env.cr.dbname):
            observations.append(signal('eta:scheduler', 'Automatic ETA processing is unavailable',
                'The reliable ETA queue is missing, disabled or failing.',
                'Review ETA Submission Jobs and its scheduled action.', 'critical'))
        elif not paused and cron.nextcall < now - timedelta(minutes=3) and not native_progress:
            observations.append(signal('eta:scheduler', 'Automatic ETA processing is overdue',
                'The ETA queue has not run on schedule.',
                'Review the scheduler and database locks.', 'critical'))
        processed = self.env['ir.config_parameter'].sudo().get_param('tradeline_eta_reliability.last_processed_at')
        try:
            processed = fields.Datetime.to_datetime(processed) if processed else None
        except (ValueError, TypeError):
            processed = None
        if (not paused and not native_progress and enabled_database == self.env.cr.dbname and processed and processed < now - timedelta(minutes=15)
                and jobs.search_count(expression.AND([daily, [
                    ('state', 'in', ['queued', 'accepted', 'unknown', 'review']),
                    '|', ('next_attempt', '=', False), ('next_attempt', '<=', now)]]))):
            observations.append(signal('eta:daily_progress', 'Daily ETA queue has stopped processing invoices',
                'New daily invoices are ready but no successful queue operation has completed for 15 minutes.',
                'Check ETA connectivity, signing, database locks and worker logs; preserve uncertain submission markers.', 'critical'))
        stalled = jobs.search_count(expression.AND([
            daily, jobs._attention_domain(now), [('state', 'in', ['queued', 'unknown', 'accepted', 'review'])],
            ['!'] + expression.normalize_domain(jobs._recorded_rejection_domain()),
            ['!'] + expression.normalize_domain(jobs._authority_duplicate_review_domain())]))
        if stalled and not paused:
            observations.append(signal('eta:daily_stalled', 'Daily ETA submissions need review',
                '%s new daily ETA invoices are delayed or have uncertain submission outcomes.' % stalled,
                'Open ETA Submission Jobs. Reconcile uncertain results before retrying.', 'critical'))
        blocked = jobs.search_count(expression.AND([daily, expression.OR([
            [('state', 'in', ['blocked', 'invalid'])], jobs._recorded_rejection_domain()])]))
        if blocked:
            observations.append(signal('eta:daily_rejected', 'Daily ETA documents need correction',
                '%s new daily ETA invoices have a validation or source-data problem.' % blocked,
                'Review ETA Submission Jobs and correct the reported document fields.'))
        return observations


class OperationsIncident(models.Model):
    _inherit = 'tradeline.operations.incident'

    is_eta_incident = fields.Boolean(compute='_compute_is_eta_incident')

    @api.depends('code')
    def _compute_is_eta_incident(self):
        for incident in self:
            incident.is_eta_incident = bool((incident.code or '').startswith('eta:'))

    def action_eta_invoice_log(self):
        self.monitor_id._ensure_admin()
        self.ensure_one()
        action = self.env['ir.actions.actions']._for_xml_id('tradeline_eta_reliability.action_eta_invoice_log')
        if self.code in ('eta:rejected', 'eta:daily_rejected'):
            action.update(domain=expression.OR([
                [('state', 'in', ['blocked', 'invalid'])],
                self.env['tradeline.eta.job']._recorded_rejection_domain()]), context={})
        elif self.code in ('eta:stalled', 'eta:daily_stalled'):
            action.update(domain=[('needs_attention', '=', True),
                                  ('state', 'in', ['queued', 'unknown', 'accepted', 'review'])], context={})
        elif self.code in ('eta:progress', 'eta:daily_progress'):
            action.update(domain=[('state', 'in', ['queued', 'unknown', 'accepted', 'review'])], context={})
        if self.code.startswith('eta:daily_'):
            action['domain'] = expression.AND([
                action.get('domain', []), self.monitor_id._eta_daily_alert_domain(fields.Datetime.now())])
        return action

from datetime import timedelta

from odoo import api, fields, models
from odoo.osv import expression
from odoo.addons.tradeline_operations_monitor.models.operations_monitor import signal
from .processing_window import EtaStopped, check_processing_window


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
        return super()._reconcile(observations, completed, now)

    def _probe_crons(self, now):
        observations = super()._probe_crons(now)
        cron = self.env.ref('tradeline_eta_reliability.cron_eta_queue', raise_if_not_found=False)
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
        elif not paused and cron.nextcall < now - timedelta(minutes=3):
            observations.append(signal('eta:scheduler', 'Automatic ETA processing is overdue',
                'The ETA queue has not run on schedule.',
                'Review the scheduler and database locks.', 'critical'))
        jobs = self.env['tradeline.eta.job'].sudo()
        processed = self.env['ir.config_parameter'].sudo().get_param('tradeline_eta_reliability.last_processed_at')
        try:
            processed = fields.Datetime.to_datetime(processed) if processed else None
        except (ValueError, TypeError):
            processed = None
        if (not paused and enabled_database == self.env.cr.dbname and processed and processed < now - timedelta(minutes=15)
                and jobs.search_count([('state', 'in', ['queued', 'accepted', 'unknown', 'review']),
                                       '|', ('next_attempt', '=', False), ('next_attempt', '<=', now)])):
            observations.append(signal('eta:progress', 'ETA queue has stopped processing invoices',
                'Invoices are ready but no successful queue operation has completed for 15 minutes.',
                'Check ETA connectivity, signing, database locks and worker logs; preserve uncertain submission markers.', 'critical'))
        stalled = jobs.search_count(expression.AND([
            jobs._attention_domain(now), [('state', 'in', ['queued', 'unknown', 'accepted', 'review'])]]))
        if stalled and not paused:
            observations.append(signal('eta:stalled', 'ETA submissions need review',
                '%s ETA jobs are delayed or have uncertain submission outcomes.' % stalled,
                'Open ETA Submission Jobs. Reconcile uncertain results before retrying.', 'critical'))
        blocked = jobs.search_count([('state', 'in', ['blocked', 'invalid'])])
        if blocked:
            observations.append(signal('eta:rejected', 'ETA documents need correction',
                '%s ETA jobs have a validation or source-data problem.' % blocked,
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
        if self.code == 'eta:rejected':
            action.update(domain=[('state', 'in', ['blocked', 'invalid'])], context={})
        elif self.code == 'eta:stalled':
            action.update(domain=[('needs_attention', '=', True),
                                  ('state', 'in', ['queued', 'unknown', 'accepted', 'review'])], context={})
        elif self.code == 'eta:progress':
            action.update(domain=[('state', 'in', ['queued', 'unknown', 'accepted', 'review'])], context={})
        return action

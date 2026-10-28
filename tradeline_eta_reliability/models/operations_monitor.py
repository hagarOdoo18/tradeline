from datetime import timedelta

from odoo import models
from odoo.addons.tradeline_operations_monitor.models.operations_monitor import signal


class OperationsMonitor(models.Model):
    _inherit = 'tradeline.operations.monitor'

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
        if not cron or not cron.active or cron.failure_count or enabled_database != self.env.cr.dbname:
            observations.append(signal('eta:scheduler', 'Automatic ETA processing is unavailable',
                'The reliable ETA queue is missing, disabled or failing.',
                'Review ETA Submission Jobs and its scheduled action.', 'critical'))
        elif cron.nextcall < now - timedelta(minutes=3):
            observations.append(signal('eta:scheduler', 'Automatic ETA processing is overdue',
                'The ETA queue has not run on schedule.',
                'Review the scheduler and database locks.', 'critical'))
        jobs = self.env['tradeline.eta.job'].sudo()
        stalled = jobs.search_count([('state', 'in', ['queued', 'unknown', 'accepted']),
                                     ('create_date', '<', now - timedelta(minutes=15))])
        if stalled:
            observations.append(signal('eta:stalled', 'ETA submissions need review',
                '%s ETA jobs are delayed or have uncertain submission outcomes.' % stalled,
                'Open ETA Submission Jobs. Reconcile uncertain results before retrying.', 'critical'))
        blocked = jobs.search_count([('state', 'in', ['blocked', 'invalid'])])
        if blocked:
            observations.append(signal('eta:rejected', 'ETA documents need correction',
                '%s ETA jobs have a validation or source-data problem.' % blocked,
                'Review ETA Submission Jobs and correct the reported document fields.'))
        return observations

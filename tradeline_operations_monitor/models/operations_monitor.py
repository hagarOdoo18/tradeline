"""Read-only business probes; monitoring never retries or changes stock jobs."""
import hmac
import logging
import re
import secrets
import uuid
from datetime import timedelta
from functools import partial
from time import monotonic
from urllib.parse import urlparse

import requests
from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError

_logger = logging.getLogger(__name__)
EMAIL = re.compile(r'^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$')
LEVEL = {'healthy': 0, 'warning': 1, 'critical': 2}
CRITICAL_CRONS = (
    'shopify_odoo_connector.ir_cron_job_cron_shopify',
    'shopify_odoo_connector.ir_cron_sync_inventory_to_shopify',
    'shopify_odoo_connector.ir_cron_sync_confirmed_orders_shopify',
    'shopify_odoo_connector.ir_cron_retry_shopify_order_events',
    'import_stock_quant_excel.ir_cron_process_stock_import',
    'tradeline_operations_monitor.cron_send_operations_alerts',
)


def signal(code, title, details, action, severity='warning', *, latched=False, version=''):
    return dict(code=code, title=title, details=details, action=action,
                severity=severity, latched=latched, evidence_version=str(version))


def shopify_log_signal(log_id, message):
    """Recognize the connector's persisted partial outcomes, not just job Done."""
    text = re.sub('<[^>]+>', ' ', str(message or ''))
    failed = re.search(r'Inventory push:.*?\b([1-9]\d*) failed\b', text, re.I)
    if failed or text.startswith('Inventory push failures:'):
        return signal('shopify:partial_inventory', 'Shopify stock push was incomplete',
            'Shopify log %s reports stock-publication errors, even if the job says Done.' % log_id,
            'Review Shopify / Logs, repair invalid mappings and verify the affected variants.',
            'critical', latched=True, version=log_id)
    if re.search(r'Creation of order .+ failed:', text, re.I):
        return signal('shopify:order_import_error', 'An individual Shopify order was not imported',
            'Shopify log %s records an order rejection inside a batch.' % log_id,
            'Review Shopify / Logs. Correct the validation problem and confirm the order is imported and reserved.',
            'critical', latched=True, version=log_id)
    return None


def send_heartbeat(url, failed=False):
    """Run only after the check commits. Never send database details or secrets."""
    try:
        response = requests.post(url + ('/fail' if failed else ''),
                                 data=b'operations check completed', timeout=(2, 3), allow_redirects=False)
        if response.status_code != 200:
            _logger.warning('External operations heartbeat was not accepted')
    except requests.RequestException:
        # Exception strings can contain the secret ping URL.
        _logger.warning('External operations heartbeat could not be delivered')


class OperationsMonitor(models.Model):
    _name = 'tradeline.operations.monitor'
    _description = 'Operations Monitoring Settings'

    name = fields.Char(required=True, default='Tradeline Operations')
    singleton = fields.Boolean(required=True, default=True, readonly=True)
    active = fields.Boolean(default=False)
    recipient_emails = fields.Char(string='Alert Recipients')
    sender_email = fields.Char(default='tradeline.odoo18@tradelinestores.com', required=True)
    warning_minutes = fields.Integer(default=3, required=True)
    critical_minutes = fields.Integer(default=10, required=True)
    lock_warning_seconds = fields.Integer(default=30, required=True)
    confirmation_checks = fields.Integer(default=2, required=True)
    reminder_minutes = fields.Integer(default=15, required=True)
    maintenance_until = fields.Datetime(help='Mute notifications during an explicitly planned maintenance window.')
    heartbeat_url = fields.Char(groups='base.group_system', copy=False,
                                help='Secret https://hc-ping.com/<UUID> URL from an external Healthchecks.io check.')
    health_token = fields.Char(groups='base.group_system', copy=False, readonly=True)
    last_checked_at = fields.Datetime(readonly=True)
    last_duration_seconds = fields.Float(readonly=True)
    last_log_id = fields.Integer(readonly=True)
    overall_status = fields.Selection([('unknown', 'Not Checked'), ('healthy', 'Healthy'),
                                       ('warning', 'Warning'), ('critical', 'Critical')],
                                      default='unknown', readonly=True)
    incident_ids = fields.One2many('tradeline.operations.incident', 'monitor_id', readonly=True)

    _sql_constraints = [('one_monitor', 'unique(singleton)', 'Only one operations monitor is supported.')]

    @api.constrains('active', 'recipient_emails', 'sender_email', 'warning_minutes', 'critical_minutes',
                    'lock_warning_seconds', 'confirmation_checks', 'reminder_minutes', 'heartbeat_url', 'singleton')
    def _validate_settings(self):
        for rec in self:
            addresses = re.split(r'[,;]', rec.recipient_emails or '')
            if any(address.strip() and not EMAIL.fullmatch(address.strip()) for address in addresses):
                raise ValidationError('Enter valid recipient email addresses separated by commas.')
            if not EMAIL.fullmatch(rec.sender_email or ''):
                raise ValidationError('Enter a valid sender email address.')
            if rec.active and not any(address.strip() for address in addresses):
                raise ValidationError('Set the alert recipients before enabling monitoring.')
            if not rec.singleton or not (1 <= rec.warning_minutes < rec.critical_minutes <= 120):
                raise ValidationError('Warning must be at least 1 minute and below the critical threshold (maximum 120).')
            if not (5 <= rec.lock_warning_seconds <= 300 and 1 <= rec.confirmation_checks <= 5
                    and 5 <= rec.reminder_minutes <= 1440):
                raise ValidationError('Check the lock, confirmation and reminder thresholds.')
            if rec.heartbeat_url:
                parsed = urlparse(rec.heartbeat_url)
                try:
                    uuid.UUID(parsed.path.lstrip('/'))
                except (ValueError, AttributeError):
                    raise ValidationError('Use the UUID-based Healthchecks.io ping URL.')
                if (parsed.scheme != 'https' or parsed.netloc != 'hc-ping.com'
                        or parsed.query or parsed.fragment):
                    raise ValidationError('Only HTTPS ping URLs on hc-ping.com are allowed.')

    def _ensure_admin(self):
        if not self.env.user.has_group('base.group_system'):
            raise AccessError('Only administrators can manage operational monitoring.')

    def action_generate_token(self):
        self._ensure_admin()
        self.ensure_one()
        self.health_token = secrets.token_urlsafe(32)

    def action_check_now(self):
        self._ensure_admin()
        self.ensure_one()
        self._run_check(notify=False)
        return True

    def action_test_email(self):
        self._ensure_admin()
        self.ensure_one()
        if not (self.recipient_emails or '').strip():
            raise ValidationError('Set the intended recipients first.')
        self._queue_email('TEST: Operations Monitoring',
                          'This is a test of the operational alert email channel. No stock or jobs were changed.')
        return True

    @api.model
    def _cron_check(self):
        for monitor in self.sudo().search([('active', '=', True)]):
            monitor._run_check(notify=True)

    def _run_check(self, notify):
        self.ensure_one()
        # Serialize this tiny monitor record only; never lock business rows.
        self.flush_recordset()
        self.env.cr.execute('SELECT id FROM tradeline_operations_monitor WHERE id=%s FOR UPDATE SKIP LOCKED', [self.id])
        if not self.env.cr.fetchone():
            return
        self.invalidate_recordset()
        started = monotonic()
        now = fields.Datetime.now()
        observations, completed = [], set()
        for name in ('database', 'crons', 'shopify', 'imports', 'mail'):
            try:
                with self.env.cr.savepoint():
                    # Scope the timeout to the probe, not later mail/history work.
                    self.env.cr.execute("SELECT current_setting('statement_timeout'), current_setting('lock_timeout')")
                    statement_timeout, lock_timeout = self.env.cr.fetchone()
                    self.env.cr.execute("SET LOCAL statement_timeout = '2000ms'")
                    self.env.cr.execute("SET LOCAL lock_timeout = '100ms'")
                    found = getattr(self, '_probe_' + name)(now)
                    self.env.cr.execute("SELECT set_config('statement_timeout', %s, true), set_config('lock_timeout', %s, true)",
                                        [statement_timeout, lock_timeout])
                observations.extend(found)
                completed.add(name)
            except Exception:
                _logger.warning('Operations probe %s could not complete', name)
                observations.append(signal('probe:' + name, 'Monitoring check unavailable: ' + name,
                                           'The bounded check failed or timed out. Health is not confirmed.',
                                           'Review server logs and database availability.', 'critical'))
        notices = self._reconcile(observations, completed, now)
        duration = monotonic() - started
        status = max((item.severity for item in self.incident_ids if item.state == 'open'),
                     key=LEVEL.get, default='healthy')
        self.write({'last_checked_at': now, 'last_duration_seconds': duration, 'overall_status': status})
        muted = bool(self.maintenance_until and self.maintenance_until > now)
        if notify and notices and not muted:
            live_notices = notices.filtered(lambda item: item.state != 'resolved')
            label = max(live_notices.mapped('severity'), key=LEVEL.get).upper() if live_notices else 'RECOVERED'
            lead = (live_notices or notices)[0].title
            subject = 'Operations %s: %s (%s incident%s)' % (label, lead, len(notices), '' if len(notices) == 1 else 's')
            self._queue_email(subject, self._email_body(notices, now), html=True)
            notices.write({'last_notified_at': now})
        if notify and self.heartbeat_url:
            self.env.cr.postcommit.add(partial(send_heartbeat, self.heartbeat_url, status == 'critical' and not muted))

    def _age_signal(self, code, title, oldest, now, action, details=''):
        if not oldest:
            return []
        oldest = fields.Datetime.to_datetime(oldest)
        seconds = max(0, (now - oldest).total_seconds())
        if seconds < self.warning_minutes * 60:
            return []
        return [signal(code, title, '%s Age: %.1f minutes.' % (details, seconds / 60), action,
                       'critical' if seconds >= self.critical_minutes * 60 else 'warning')]

    def _probe_database(self, now):
        self.env.cr.execute(r"""
            SELECT a.pid, a.state, a.wait_event_type, pg_blocking_pids(a.pid),
                   EXTRACT(EPOCH FROM (clock_timestamp() - query_start)),
                   EXTRACT(EPOCH FROM (clock_timestamp() - state_change)),
                   (query ~* '^\s*WITH last_cron_progress AS\s*\('
                    AND query LIKE '%%FROM ir_cron_progress%%'
                    AND NOT EXISTS (SELECT 1 FROM pg_locks l WHERE l.pid=a.pid
                                    AND l.granted AND l.locktype='advisory')
                    AND NOT EXISTS (
                        SELECT 1 FROM pg_locks l JOIN pg_class c ON c.oid=l.relation
                         WHERE l.pid=a.pid AND l.granted AND c.relkind IN ('r','p','m')
                           AND c.relname NOT IN ('ir_cron', 'ir_cron_progress',
                                                 'ir_cron_trigger', 'ir_module_module')
                    )),
                   backend_start,
                   substring(query FROM '(?i)(?:FROM|UPDATE|INTO)\s+"?([a-z_][a-z_0-9]*)')
              FROM pg_stat_activity a
             WHERE datname=current_database() AND pid<>pg_backend_pid()
        """)
        observations = []
        sessions = self.env.cr.fetchall()
        blocking_pids = {pid for row in sessions for pid in row[3]}
        for pid, state, wait, blockers, query_age, state_age, coordinator, backend_start, table in sessions:
            context = ' Backend started: %s. Last SQL relation: %s.' % (backend_start, table or 'unknown')
            if wait == 'Lock' and blockers and (query_age or 0) >= self.lock_warning_seconds:
                observations.append(signal('database:blocked:%s' % pid, 'Database work is blocked',
                    'Session %s has waited %.0fs; blocking sessions: %s.' % (pid, query_age, ', '.join(map(str, blockers))) + context,
                    'Inspect the blocking transaction in Odoo.sh. Do not terminate sessions blindly.',
                    'critical' if query_age >= 120 else 'warning'))
            if state in ('idle in transaction', 'idle in transaction (aborted)') and (state_age or 0) >= 60:
                # Odoo 18 holds the acquired cron row on cron_cr while the
                # callback runs on job_cr. This coordination cursor is normally
                # idle. Exempt only that known query, with metadata-only locks,
                # no blocked peers, and for less than the critical job budget.
                if (state == 'idle in transaction' and coordinator
                        and pid not in blocking_pids and state_age < self.critical_minutes * 60):
                    continue
                observations.append(signal('database:idle:%s' % pid, 'Transaction held open while idle',
                    'Session %s has been idle inside a transaction for %.0fs.' % (pid, state_age) + context,
                    'Check long Shopify requests and uncommitted inventory work.',
                    'critical' if state_age >= 180 else 'warning'))
            if state == 'active' and wait != 'Lock' and (query_age or 0) >= 60:
                observations.append(signal('database:slow:%s' % pid, 'Long-running database query',
                    'Session %s has an active query older than %.0fs.' % (pid, query_age),
                    'Review the query and server CPU/I/O before changing configuration.',
                    'critical' if query_age >= 300 else 'warning'))
        self.env.cr.execute("SELECT count(*), current_setting('max_connections')::integer FROM pg_stat_activity")
        connections, capacity = self.env.cr.fetchone()
        if connections >= capacity * .85:
            observations.append(signal('database:connections', 'Database connections nearing capacity',
                '%s of %s PostgreSQL connections are in use.' % (connections, capacity),
                'Inspect stuck requests and connection pooling.', 'critical' if connections >= capacity * .95 else 'warning'))
        return observations

    def _probe_crons(self, now):
        observations = []
        for xmlid in CRITICAL_CRONS:
            cron = self.env.ref(xmlid, raise_if_not_found=False)
            if not cron:
                expected = (xmlid.startswith('shopify_odoo_connector.') and 'job.cron' in self.env.registry
                            or xmlid.startswith('import_stock_quant_excel.') and 'import.stock.quant.wizard' in self.env.registry
                            or xmlid.startswith('tradeline_operations_monitor.'))
                if expected:
                    observations.append(signal('crons:' + xmlid, 'Critical scheduled action is missing', xmlid,
                        'Restore the scheduled action from its module and review why it was removed.', 'critical'))
                continue
            code = 'crons:' + xmlid
            if not cron.active:
                observations.append(signal(code, 'Critical scheduled action is disabled', cron.name,
                    'Check whether the pause was intentional and review failures before resuming.', 'critical'))
            elif cron.failure_count:
                observations.append(signal(code, 'Scheduled action is failing',
                    '%s: %s consecutive failures.' % (cron.name, cron.failure_count),
                    'Review the scheduled action and its server traceback.', 'critical'))
            else:
                observations.extend(self._age_signal(code, 'Scheduled action is overdue', cron.nextcall, now,
                    'Review the scheduler, queue progress and database blocking.', cron.name))
        return observations

    def _probe_shopify(self, now):
        observations = []
        if 'job.cron' in self.env.registry:
            for function in ('export_inventory_to_shopify', 'import_confirmed_orders_from_shopify',
                             'import_draft_orders_from_shopify'):
                jobs = self.env['job.cron'].sudo().with_context(prefetch_fields=False)
                first = jobs.search([('function', '=', function), ('state', '=', 'pending')], order='create_date,id', limit=1)
                observations.extend(self._age_signal('shopify:queue:' + function, 'Shopify queue is delayed',
                    first.create_date, now, 'Review Shopify / Jobs and check database blockers.', function))
                failed = jobs.search([('function', '=', function), ('state', '=', 'failed'),
                                      ('write_date', '>=', now - timedelta(days=1))], order='write_date desc,id desc', limit=1)
                if failed:
                    observations.append(signal('shopify:failed:' + function, 'Shopify job failed',
                        'Job %s (%s) needs review.' % (failed.id, function),
                        'Review this job and Shopify logs; a new successful job does not prove every old item was repaired.',
                        'critical', latched=True, version='%s:%s' % (failed.id, failed.write_date)))
        if 'shopify.order.event' in self.env.registry:
            events = self.env['shopify.order.event'].sudo().with_context(prefetch_fields=False)
            first = events.search([('state', 'in', ['received', 'processing'])], order='received_at,id', limit=1)
            observations.extend(self._age_signal('shopify:events', 'Shopify order reservation is delayed', first.received_at,
                now, 'Review Shopify / Order Events. Accepted sales may not yet be reserved in Odoo.'))
            bad = events.search([('state', 'in', ['blocked', 'failed'])], order='write_date desc,id desc', limit=1)
            if bad:
                observations.append(signal('shopify:order_rejected', 'Shopify order needs attention',
                    'Order event %s is %s.' % (bad.id, bad.state),
                    'Review the event rejection and verify its order/stock reservation.', 'critical'))
        if 'log.message' in self.env.registry:
            Log = self.env['log.message'].sudo().with_context(prefetch_fields=False)
            latest = Log.search([], order='id desc', limit=1)
            cursor = self.last_log_id or max(0, latest.id - 500)
            # The legacy log table has no date/model index. Filtering it from
            # ID zero by date and HTML text scans years of history. Walk a
            # bounded PK tail instead, then classify the few relevant rows.
            logs = Log.search_read([('id', '>', cursor)],
                                   ['name', 'model', 'create_date'], order='id', limit=500)
            for log in logs:
                if (log['model'] not in ('Stock Quantity', 'sale.order')
                        or fields.Datetime.to_datetime(log['create_date']) < now - timedelta(days=1)):
                    continue
                observation = shopify_log_signal(log['id'], log['name'])
                if observation:
                    observations.append(observation)
            if logs:
                self.last_log_id = logs[-1]['id']
            if len(logs) == 500 and logs[-1]['id'] < latest.id:
                observations.append(signal('shopify:log_scan_backlog', 'Error-log scan is catching up',
                    'Recent persisted logs are being checked in bounded batches; the latest entries are not yet verified.',
                    'Review Shopify / Logs directly for urgent failures while the monitor catches up.'))
        return observations

    def _probe_imports(self, now):
        if 'import.stock.quant.wizard' not in self.env.registry:
            return []
        observations = []
        imports = self.env['import.stock.quant.wizard'].sudo().search_read(
            [('state', 'in', ['processing', 'failed'])], ['state', 'remaining_rows', 'write_date'], limit=100)
        for job in imports:
            if job['state'] == 'failed':
                observations.append(signal('imports:%s' % job['id'], 'Excel stock import paused',
                    'Import %s has %s remaining rows.' % (job['id'], job['remaining_rows']),
                    'Open Products / Stock Import History, inspect the error, and resume the same import.', 'critical'))
            else:
                observations.extend(self._age_signal('imports:%s' % job['id'], 'Excel stock import made no recent progress',
                    job['write_date'], now, 'Review import progress and scheduled-action/database health.',
                    'Import %s: %s remaining rows.' % (job['id'], job['remaining_rows'])))
        # Multi Stock Update is one atomic transaction: an old Draft is not
        # proof of a stalled operation. Its live blocking/slow SQL is covered
        # by database probes, without flagging legitimate saved drafts.
        return observations

    def _probe_mail(self, now):
        observations = []
        pending = self.env['mail.mail'].sudo().with_context(prefetch_fields=False).search([
            ('operations_monitor_id', '=', self.id), ('state', '=', 'outgoing'),
        ], order='create_date,id', limit=1)
        observations.extend(self._age_signal('mail:alert_queue', 'Alert email delivery is delayed',
            pending.create_date, now, 'Check Operations: Deliver Alert Emails and the outgoing mail server.'))
        failed = self.env['mail.mail'].sudo().with_context(prefetch_fields=False).search([
            ('state', '=', 'exception'), ('write_date', '>=', now - timedelta(hours=1)),
        ], order='write_date desc,id desc', limit=1)
        if failed:
            observations.append(signal('mail:failed', 'An outgoing email could not be sent',
                'Mail record %s failed. Operational or executive messages may be affected.' % failed.id,
                'Check outgoing mail errors and the configured SMTP server.', 'critical', latched=True,
                version='%s:%s' % (failed.id, failed.write_date)))
        if 'tradeline.executive.report.history' in self.env.registry:
            failed = self.env['tradeline.executive.report.history'].sudo().search([
                ('state', '=', 'failed'), ('write_date', '>=', now - timedelta(days=1)),
            ], order='write_date desc,id desc', limit=1)
            if failed:
                observations.append(signal('mail:executive', 'Executive report delivery failed',
                    'Report history %s needs review.' % failed.id,
                    'Open Executive Report History and check the report/mail error.', 'critical', latched=True,
                    version='%s:%s' % (failed.id, failed.write_date)))
        return observations

    def _reconcile(self, observations, completed, now):
        Incident = self.env['tradeline.operations.incident'].sudo()
        previous = {item.code: item for item in self.incident_ids}
        # Several log rows can report the same failure in one scan. Keep the
        # latest evidence, while generating one incident/email summary.
        by_code = {item['code']: item for item in observations}
        notices = Incident.browse()
        for code, item in by_code.items():
            record = previous.get(code)
            if record and record.state == 'resolved' and item['latched'] and record.evidence_version == item['evidence_version']:
                continue
            values = dict(item, monitor_id=self.id, last_seen_at=now)
            if not record:
                record = Incident.create(dict(values, first_seen_at=now, consecutive_checks=1, state='observing'))
            else:
                values['consecutive_checks'] = record.consecutive_checks + 1 if record.state != 'resolved' else 1
                if record.state == 'resolved':
                    values.update(state='observing', first_seen_at=now, last_notified_at=False, resolved_at=False)
                record.write(values)
            old_severity = record.notified_severity
            confirmed = item['latched'] or record.consecutive_checks >= self.confirmation_checks
            if confirmed:
                record.state = 'open'
                if (not record.last_notified_at or LEVEL[record.severity] > LEVEL.get(old_severity, -1)
                        or (now - record.last_notified_at).total_seconds() >= self.reminder_minutes * 60):
                    notices |= record
                    record.notified_severity = record.severity
        for code, record in previous.items():
            source = 'crons' if code.startswith('crons:') else code.split(':', 1)[0]
            if code.startswith('probe:'):
                source = code.split(':', 1)[1]
            if code not in by_code and source in completed and not record.latched and record.state != 'resolved':
                if record.state == 'open':
                    notices |= record
                record.write({'state': 'resolved', 'resolved_at': now, 'consecutive_checks': 0})
        return notices

    def _queue_email(self, subject, body, html=False):
        self.ensure_one()
        mail = self.env['mail.mail'].sudo().create({
            'subject': '[Tradeline] ' + subject,
            'email_from': self.sender_email, 'email_to': self.recipient_emails.replace(';', ','),
            'body_html': body if html else Markup('<p>%s</p>') % body,
            'auto_delete': False, 'operations_monitor_id': self.id,
        })
        # A separate sender keeps SMTP/network waits out of the health check.
        self.env.ref('tradeline_operations_monitor.cron_send_operations_alerts')._trigger()
        return mail

    alert_mail_ids = fields.One2many('mail.mail', 'operations_monitor_id', readonly=True)

    def _email_body(self, incidents, now):
        base = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '')
        local_time = fields.Datetime.context_timestamp(self.with_context(tz='Africa/Cairo'), now)
        rows = []
        for item in incidents[:20]:
            rows.append(Markup('<li><strong>%s: %s</strong><br/>%s<br/>Next: %s</li>') % (
                'RECOVERED' if item.state == 'resolved' else item.severity.upper(),
                item.title, item.details, item.action))
        link = '%s/odoo/action-tradeline_operations_monitor.action_operations_incidents' % base.rstrip('/')
        return Markup('<p>Operations check: %s (Cairo). Review sales/stock delays promptly.</p><ul>%s</ul>'
                      '<p><a href="%s">Open Operations Incidents</a></p>'
                      '<p>Monitoring reports symptoms; it does not stop sales, alter inventory, or guarantee prevention of overselling.</p>') % (
                          local_time.strftime('%d %b %Y %H:%M:%S %Z'), Markup('').join(rows), link)

    @api.model
    def _cron_send_alerts(self):
        monitors = self.sudo().with_context(active_test=False).search([])
        mails = self.env['mail.mail'].sudo().search([
            ('operations_monitor_id', 'in', monitors.ids), ('state', '=', 'outgoing'),
        ], order='id', limit=10)
        if mails:
            mails.send(raise_exception=False)

    def _health_payload(self, token, now=None):
        self.ensure_one()
        if (not self.active or not self.health_token
                or not hmac.compare_digest(self.health_token.encode('utf-8'), (token or '').encode('utf-8'))):
            return {'status': 'unauthorized'}, 401
        now = now or fields.Datetime.now()
        if self.maintenance_until and self.maintenance_until > now:
            return {'status': 'maintenance'}, 200
        stale = not self.last_checked_at or (now - self.last_checked_at).total_seconds() > 180
        status = 'critical' if stale else self.overall_status
        return {'status': status, 'heartbeat_stale': stale,
                'last_checked_at': fields.Datetime.to_string(self.last_checked_at) if self.last_checked_at else None}, (
                    503 if status in ('critical', 'unknown') else 200)


class OperationsIncident(models.Model):
    _name = 'tradeline.operations.incident'
    _description = 'Operations Incident'
    _order = 'last_seen_at desc,id desc'
    _rec_name = 'title'

    monitor_id = fields.Many2one('tradeline.operations.monitor', required=True, ondelete='cascade', index=True)
    code = fields.Char(required=True, index=True)
    title = fields.Char(required=True)
    details = fields.Text()
    action = fields.Text(string='Recommended Action')
    severity = fields.Selection([('warning', 'Warning'), ('critical', 'Critical')], required=True)
    state = fields.Selection([('observing', 'Confirming'), ('open', 'Open'), ('resolved', 'Resolved')], default='observing', index=True)
    latched = fields.Boolean(help='An individual failure needs explicit review; it cannot be cleared by the absence of new errors.')
    evidence_version = fields.Char()
    consecutive_checks = fields.Integer(default=0)
    first_seen_at = fields.Datetime()
    last_seen_at = fields.Datetime()
    resolved_at = fields.Datetime()
    last_notified_at = fields.Datetime()
    notified_severity = fields.Char()

    _sql_constraints = [('incident_code', 'unique(monitor_id, code)', 'An incident code is unique per monitor.')]

    def action_resolve(self):
        if not self.env.user.has_group('base.group_system'):
            raise AccessError('Only administrators can resolve operational incidents.')
        self.write({'state': 'resolved', 'resolved_at': fields.Datetime.now(), 'consecutive_checks': 0})


class OperationsAlertMail(models.Model):
    _inherit = 'mail.mail'

    operations_monitor_id = fields.Many2one('tradeline.operations.monitor', index=True, copy=False,
                                           groups='base.group_system', ondelete='set null')

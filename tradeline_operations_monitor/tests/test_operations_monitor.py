from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import TransactionCase, new_test_user, tagged
from odoo.tools import mute_logger

from ..models.operations_monitor import signal, send_heartbeat, shopify_log_signal


@tagged('post_install', '-at_install')
class TestOperationsMonitor(TransactionCase):
    def setUp(self):
        super().setUp()
        self.monitor = self.env.ref('tradeline_operations_monitor.monitor_default')
        self.monitor.write({'recipient_emails': 'operator@example.com,hagar@example.com'})
        self.now = fields.Datetime.now()
        self.problem = signal('shopify:test', 'Shopify queue delayed', 'Oldest job is 6 minutes old.',
                              'Review Shopify / Jobs.')

    def test_transient_failure_does_not_email_and_recovery_is_quiet(self):
        self.assertFalse(self.monitor._reconcile([self.problem], {'shopify'}, self.now))
        incident = self.monitor.incident_ids
        self.assertEqual(incident.state, 'observing')
        self.assertFalse(self.monitor._reconcile([], {'shopify'}, self.now + timedelta(minutes=1)))
        self.assertEqual(incident.state, 'resolved')

    def test_confirmation_escalation_reminder_and_recovery(self):
        self.monitor._reconcile([self.problem], {'shopify'}, self.now)
        incident = self.monitor._reconcile([self.problem], {'shopify'}, self.now + timedelta(minutes=1))
        self.assertEqual(incident.state, 'open')
        incident.last_notified_at = self.now + timedelta(minutes=1)
        self.assertFalse(self.monitor._reconcile([self.problem], {'shopify'}, self.now + timedelta(minutes=2)))
        critical = dict(self.problem, severity='critical')
        self.assertEqual(self.monitor._reconcile([critical], {'shopify'}, self.now + timedelta(minutes=3)), incident)
        incident.last_notified_at = self.now + timedelta(minutes=3)
        self.assertEqual(self.monitor._reconcile([critical], {'shopify'}, self.now + timedelta(minutes=19)), incident)
        self.assertEqual(self.monitor._reconcile([], {'shopify'}, self.now + timedelta(minutes=20)), incident)
        self.assertEqual(incident.state, 'resolved')

    def test_partial_failure_stays_open_until_reviewed_and_new_evidence_reopens(self):
        event = dict(self.problem, severity='critical', latched=True, evidence_version='101')
        incident = self.monitor._reconcile([event], {'shopify'}, self.now)
        self.assertEqual(incident.state, 'open')
        self.monitor._reconcile([], {'shopify'}, self.now + timedelta(minutes=1))
        self.assertEqual(incident.state, 'open')
        incident.action_resolve()
        self.assertFalse(self.monitor._reconcile([event], {'shopify'}, self.now + timedelta(minutes=2)))
        self.assertEqual(incident.state, 'resolved')
        self.assertEqual(self.monitor._reconcile([dict(event, evidence_version='102')], {'shopify'},
                                                self.now + timedelta(minutes=3)), incident)
        self.assertEqual(incident.state, 'open')

    def test_unavailable_probe_cannot_report_business_problem_recovered(self):
        self.monitor._reconcile([dict(self.problem, latched=True)], {'shopify'}, self.now)
        incident = self.monitor.incident_ids
        incident.latched = False
        self.monitor._reconcile([], {'database'}, self.now + timedelta(minutes=1))
        self.assertEqual(incident.state, 'open')

    def test_health_endpoint_summary_is_authenticated_and_detects_dead_scheduler(self):
        self.monitor.write({'active': True, 'health_token': 'test-secret',
                            'last_checked_at': self.now, 'overall_status': 'healthy'})
        self.assertEqual(self.monitor._health_payload('incorrect', self.now)[1], 401)
        self.assertEqual(self.monitor._health_payload('incorrect-é', self.now)[1], 401)
        self.assertEqual(self.monitor._health_payload('test-secret', self.now)[1], 200)
        payload, code = self.monitor._health_payload('test-secret', self.now + timedelta(minutes=4))
        self.assertEqual(code, 503)
        self.assertTrue(payload['heartbeat_stale'])
        self.assertNotIn('health_token', payload)
        self.assertNotIn('recipient_emails', payload)

    def test_settings_reject_missing_recipients_and_private_heartbeat_destinations(self):
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self.monitor.write({'recipient_emails': '', 'active': True})
        for url in ('http://hc-ping.com/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                    'https://127.0.0.1/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                    'https://hc-ping.com@localhost/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'):
            with self.assertRaises(ValidationError), self.cr.savepoint():
                self.monitor.heartbeat_url = url

    def test_maintenance_mutes_email_and_health_but_keeps_success_heartbeat(self):
        self.monitor.write({'active': True, 'health_token': 'test-secret',
                            'heartbeat_url': 'https://hc-ping.com/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                            'maintenance_until': self.now + timedelta(hours=1)})
        problem = dict(self.problem, latched=True, severity='critical')
        probes = {name: (lambda record, now: []) for name in
                  ('_probe_database', '_probe_crons', '_probe_imports', '_probe_mail')}
        probes['_probe_shopify'] = lambda record, now: [problem]
        with patch.multiple(type(self.monitor), **probes):
            self.monitor._run_check(notify=True)
        self.assertFalse(self.monitor.alert_mail_ids)
        self.assertEqual(self.monitor._health_payload('test-secret', self.now), ({'status': 'maintenance'}, 200))

    def test_email_queue_is_escaped_and_uses_only_selected_recipients(self):
        incident = self.monitor._reconcile([dict(self.problem, title='<script>unsafe</script>', latched=True)],
                                          {'shopify'}, self.now)
        mail = self.monitor._queue_email('Warning', self.monitor._email_body(incident, self.now), html=True)
        self.assertEqual(mail.email_to, 'operator@example.com,hagar@example.com')
        self.assertIn('&lt;script&gt;', mail.body_html)
        self.assertNotIn('<script>', mail.body_html)
        self.assertFalse(mail.attachment_ids)
        self.assertEqual(mail.state, 'outgoing')

    def test_alert_sender_does_not_drain_unrelated_business_mail(self):
        alert = self.monitor._queue_email('Test', 'Test')
        ordinary = self.env['mail.mail'].create({'subject': 'Unrelated', 'email_to': 'other@example.com', 'body_html': '<p>Other</p>'})
        sent = []
        with patch.object(type(alert), 'send', lambda mails, **kwargs: sent.extend(mails.ids)):
            self.monitor._cron_send_alerts()
        self.assertEqual(sent, alert.ids)
        self.assertNotIn(ordinary.id, sent)

    def test_check_queues_one_warning_then_one_recovery_without_repeats(self):
        probes = {name: (lambda record, now: []) for name in
                  ('_probe_database', '_probe_crons', '_probe_imports', '_probe_mail')}
        probes['_probe_shopify'] = lambda record, now: [self.problem]
        with patch.multiple(type(self.monitor), **probes):
            self.monitor._run_check(notify=True)
            self.assertFalse(self.monitor.alert_mail_ids)
            self.monitor._run_check(notify=True)
            self.assertEqual(len(self.monitor.alert_mail_ids), 1)
            self.monitor._run_check(notify=True)
            self.assertEqual(len(self.monitor.alert_mail_ids), 1)
        probes['_probe_shopify'] = lambda record, now: []
        with patch.multiple(type(self.monitor), **probes):
            self.monitor._run_check(notify=True)
        self.assertEqual(len(self.monitor.alert_mail_ids), 2)
        self.assertTrue(any('RECOVERED' in mail.subject for mail in self.monitor.alert_mail_ids))

    def test_admin_only_settings_and_incident_resolution(self):
        employee = new_test_user(self.env(context=dict(self.env.context, no_reset_password=True)),
                                 login='ops_regular_employee', groups='base.group_user')
        with self.assertRaises(AccessError):
            self.monitor.with_user(employee).action_generate_token()
        incident = self.monitor._reconcile([dict(self.problem, latched=True)], {'shopify'}, self.now)
        with self.assertRaises(AccessError):
            incident.with_user(employee).action_resolve()

    @mute_logger('odoo.addons.tradeline_operations_monitor.models.operations_monitor', 'odoo.sql_db')
    def test_probe_timeout_isolated_and_connection_settings_restored(self):
        self.env.cr.execute("SELECT current_setting('statement_timeout'), current_setting('lock_timeout')")
        original = self.env.cr.fetchone()

        def timeout(record, now):
            record.env.cr.execute("SET LOCAL statement_timeout = '5ms'")
            record.env.cr.execute('SELECT pg_sleep(0.05)')

        with patch.object(type(self.monitor), '_probe_database', timeout):
            self.monitor._run_check(notify=False)
        incident = self.monitor.incident_ids.filtered(lambda item: item.code == 'probe:database')
        self.assertTrue(incident)
        self.assertEqual(incident.severity, 'critical')
        self.env.cr.execute("SELECT current_setting('statement_timeout'), current_setting('lock_timeout')")
        self.assertEqual(self.env.cr.fetchone(), original)

    def test_real_database_probe_executes_without_locking_business_tables(self):
        self.assertIsInstance(self.monitor._probe_database(self.now), list)
        self.env.cr.execute("SELECT count(*) FROM pg_locks WHERE pid=pg_backend_pid() AND mode='AccessExclusiveLock'")
        self.assertEqual(self.env.cr.fetchone()[0], 0)

    def test_disabled_alert_sender_is_reported_critical(self):
        cron = self.env.ref('tradeline_operations_monitor.cron_send_operations_alerts')
        cron.active = False
        observations = self.monitor._probe_crons(self.now)
        matches = [item for item in observations if item['code'].endswith('cron_send_operations_alerts')]
        self.assertEqual(matches[0]['severity'], 'critical')

    def test_external_heartbeat_uses_timeout_no_redirect_and_no_private_payload(self):
        with patch('odoo.addons.tradeline_operations_monitor.models.operations_monitor.requests.post') as post:
            post.return_value.status_code = 200
            send_heartbeat('https://hc-ping.com/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', failed=True)
            self.assertTrue(post.call_args.args[0].endswith('/fail'))
            self.assertEqual(post.call_args.kwargs['timeout'], (2, 3))
            self.assertFalse(post.call_args.kwargs['allow_redirects'])
            self.assertEqual(post.call_args.kwargs['data'], b'operations check completed')

    def test_done_batch_partial_errors_and_individual_order_rejection_are_detected(self):
        partial = shopify_log_signal(123, 'Inventory push: 11067 level(s) updated, 0 unchanged (skipped), 1 failed (5 group(s), 2 location(s)) in 24.6s via GraphQL')
        self.assertEqual(partial['code'], 'shopify:partial_inventory')
        self.assertTrue(partial['latched'])
        rejection = shopify_log_signal(124, '<p>Creation of order #1284 failed: Tax is required.</p>')
        self.assertEqual(rejection['code'], 'shopify:order_import_error')
        self.assertNotIn('Tax is required', rejection['details'])
        self.assertIsNone(shopify_log_signal(125, 'Inventory push: 11067 level(s) updated, 0 unchanged (skipped), 0 failed (5 group(s), 2 location(s)) in 24.6s via GraphQL'))

    def test_unrelated_incident_has_no_shopify_log(self):
        incident = self.monitor._reconcile([dict(self.problem, latched=True)], {'shopify'}, self.now)
        self.assertFalse(incident.source_log_excerpt)
        self.assertFalse(incident.source_log_url)

    def test_existing_incident_reads_original_log_without_rescan_or_email_disclosure(self):
        if 'log.message' not in self.env.registry:
            self.skipTest('Shopify connector is not installed')
        log = self.env['log.message'].sudo().create({
            'name': '<p>Creation of order #1336 failed: Please Set Mobile Number</p>'
                    '<p>Customer &amp; delivery details.</p>',
            'model': 'sale.order',
        })
        observation = shopify_log_signal(log.id, log.name)
        incident = self.monitor._reconcile([observation], {'shopify'}, self.now)
        cursor = self.monitor.last_log_id
        self.assertIn('Shopify log %s' % log.id, incident.source_log_excerpt)
        self.assertIn('Creation of order #1336 failed: Please Set Mobile Number', incident.source_log_excerpt)
        self.assertIn('\nCustomer & delivery details.', incident.source_log_excerpt)
        self.assertNotIn('<p>', incident.source_log_excerpt)
        self.assertEqual(incident.source_log_url, '/odoo/log.message/%s' % log.id)
        self.assertEqual(self.monitor.last_log_id, cursor)
        self.assertNotIn('Please Set Mobile Number', self.monitor._email_body(incident, self.now))
        # Reopening an existing incident derives its log from saved evidence.
        incident.invalidate_recordset(['source_log_excerpt', 'source_log_url'])
        self.assertIn('Please Set Mobile Number', incident.source_log_excerpt)
        incident.invalidate_recordset(['source_log_excerpt', 'source_log_url'])
        with patch.object(type(log), 'has_access', return_value=False):
            self.assertFalse(incident.source_log_excerpt)
            self.assertFalse(incident.source_log_url)
        log.unlink()
        incident.invalidate_recordset(['source_log_excerpt', 'source_log_url'])
        self.assertIn('unavailable', incident.source_log_excerpt)
        self.assertFalse(incident.source_log_url)

    def test_source_log_tracks_latest_evidence_and_truncates_long_messages(self):
        if 'log.message' not in self.env.registry:
            self.skipTest('Shopify connector is not installed')
        logs = self.env['log.message'].sudo().create([
            {'name': '<p>Creation of order #1336 failed: Please Set Mobile Number</p>', 'model': 'sale.order'},
            {'name': '<p>Creation of order #1337 failed: %s</p>' % ('x' * 9000), 'model': 'sale.order'},
        ])
        incident = self.monitor._reconcile([shopify_log_signal(logs[0].id, logs[0].name)], {'shopify'}, self.now)
        self.assertIn('#1336', incident.source_log_excerpt)
        self.monitor._reconcile([shopify_log_signal(logs[1].id, logs[1].name)], {'shopify'}, self.now)
        self.assertIn('#1337', incident.source_log_excerpt)
        self.assertNotIn('#1336', incident.source_log_excerpt)
        self.assertIn('[Message truncated.', incident.source_log_excerpt)
        self.assertLess(len(incident.source_log_excerpt), 8200)
        self.assertEqual(incident.source_log_url, '/odoo/log.message/%s' % logs[1].id)
        for invalid in ('not-a-log', '123:2026-10-21', '-1', '0', '2147483648', '9' * 5000, '١٢٣', ''):
            incident.evidence_version = invalid
            self.assertFalse(incident.source_log_excerpt)
            self.assertFalse(incident.source_log_url)

    def test_stalled_excel_import_and_paused_import_are_detected_without_opening_file(self):
        if 'import.stock.quant.wizard' not in self.env.registry:
            self.skipTest('Excel importer is not installed')
        wizard = self.env['import.stock.quant.wizard'].create({
            'file': b'dGVzdA==', 'filename': 'monitor-test.xlsx', 'state': 'processing', 'remaining_rows': 100,
        })
        wizard.flush_recordset()
        self.env.cr.execute('UPDATE import_stock_quant_wizard SET write_date=%s WHERE id=%s',
                            [self.now - timedelta(minutes=12), wizard.id])
        wizard.invalidate_recordset()
        observations = self.monitor._probe_imports(self.now)
        self.assertEqual(observations[0]['severity'], 'critical')
        wizard.state = 'failed'
        observations = self.monitor._probe_imports(self.now)
        self.assertEqual(observations[0]['title'], 'Excel stock import paused')

    def test_database_lock_chain_and_idle_transaction_are_distinguished(self):
        self.monitor.read(['lock_warning_seconds'])
        fetches = iter([[(123, 'active', 'Lock', [456], 180, 180, False, self.now, 'stock_quant'),
                         (456, 'idle in transaction', 'Client', [], 250, 200, False, self.now, 'stock_quant')], (20, 100)])
        with patch.object(type(self.env.cr), 'execute', lambda *args, **kwargs: None), \
                patch.object(type(self.env.cr), 'fetchall', lambda cr: next(fetches), create=True), \
                patch.object(type(self.env.cr), 'fetchone', lambda cr: next(fetches), create=True):
            observations = self.monitor._probe_database(self.now)
        self.assertEqual({item['code'] for item in observations}, {'database:blocked:123', 'database:idle:456'})
        self.assertTrue(all(item['severity'] == 'critical' for item in observations))

    def test_cron_coordination_is_exempt_only_without_blocked_peers_and_within_budget(self):
        self.monitor.read(['lock_warning_seconds', 'critical_minutes'])
        rows = [(101, 'idle in transaction', 'Client', [], 240, 240, True, self.now, 'ir_cron_progress'),
                (102, 'idle in transaction', 'Client', [], 240, 240, True, self.now, 'ir_cron_progress'),
                (103, 'active', 'Lock', [102], 180, 180, False, self.now, 'ir_cron'),
                (104, 'idle in transaction', 'Client', [], 601, 601, True, self.now, 'ir_cron_progress'),
                (105, 'idle in transaction (aborted)', 'Client', [], 240, 240, True, self.now, 'ir_cron_progress'),
                (106, 'idle in transaction', 'Client', [], 240, 240, False, self.now, 'stock_quant')]
        fetches = iter([rows, (20, 100)])
        with patch.object(type(self.env.cr), 'execute', lambda *args, **kwargs: None), \
                patch.object(type(self.env.cr), 'fetchall', lambda cr: next(fetches), create=True), \
                patch.object(type(self.env.cr), 'fetchone', lambda cr: next(fetches), create=True):
            observations = self.monitor._probe_database(self.now)
        self.assertEqual({item['code'] for item in observations}, {
            'database:idle:102', 'database:blocked:103', 'database:idle:104',
            'database:idle:105', 'database:idle:106'})
        self.assertTrue(all(item['severity'] == 'critical' for item in observations))
        self.assertIn('stock_quant', observations[-1]['details'])

    def test_app_menus_only_visible_to_builtin_administrator(self):
        administrator = self.env.ref('base.user_admin')
        other_admin = new_test_user(self.env(context=dict(self.env.context, no_reset_password=True)),
                                    login='ops_other_administrator', groups='base.group_system')
        other_admin.groups_id = administrator.groups_id
        root = self.env.ref('tradeline_operations_monitor.menu_operations_root')
        menus = self.env['ir.ui.menu']
        app_ids = set(menus.with_context(**{'ir.ui.menu.full_list': True}).search([
            ('id', 'child_of', root.id),
        ]).ids)
        for debug in (False, True):
            with self.subTest(debug=debug):
                admin_menus = menus.with_user(administrator)
                other_menus = menus.with_user(other_admin)
                self.assertTrue(app_ids <= admin_menus._visible_menu_ids(debug=debug))
                self.assertFalse(app_ids & other_menus._visible_menu_ids(debug=debug))
                self.assertTrue(app_ids <= admin_menus._visible_menu_ids(debug=debug))
                self.assertFalse(app_ids & set(other_menus.load_menus(debug)))
                self.assertIn(self.env.ref('base.menu_administration').id,
                              other_menus._visible_menu_ids(debug=debug))

        other_admin.groups_id = self.env.ref('base.group_user')
        self.assertFalse(app_ids & menus.with_user(other_admin)._visible_menu_ids())

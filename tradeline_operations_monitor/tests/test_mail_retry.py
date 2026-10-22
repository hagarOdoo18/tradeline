from datetime import timedelta
from smtplib import SMTPAuthenticationError, SMTPDataError
from unittest.mock import patch

from odoo import fields
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger

from ..models.mail_retry import RETRY_MINUTES, temporary_smtp_rejection


@tagged('post_install', '-at_install')
class TestTemporaryMailRetry(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Mail = self.env['mail.mail']
        self.Server = self.env['ir.mail_server']
        self.monitor = self.env.ref('tradeline_operations_monitor.monitor_default')
        self.now = fields.Datetime.now()
        self.reason = "Mail Delivery Failed\nSMTPDataError: (451, b'4.3.0 Mail server temporarily rejected message')"

    def _mail(self, **values):
        return self.Mail.create(dict({
            'subject': 'Temporary SMTP rejection test', 'email_from': 'sender@example.com',
            'email_to': 'recipient@example.com', 'body_html': '<p>Test only</p>',
            'auto_delete': False,
        }, **values))

    def _failure(self, mail, reason=None, success_pids=None):
        mail.write({'state': 'exception', 'failure_reason': reason or self.reason})
        mail._postprocess_sent_message(success_pids or [], failure_reason=reason or self.reason,
                                       failure_type='unknown')

    def test_classification_requires_explicit_temporary_smtp_response(self):
        for reason in (self.reason, "SMTPConnectError: (421, b'try later')", "(450, b'mailbox busy')"):
            self.assertTrue(temporary_smtp_rejection(reason), reason)
        for reason in ('', 'Connection timed out', 'SMTPServerDisconnected', 'Order 451 failed',
                       "SMTPDataError: (550, b'5.1.1 unknown recipient')",
                       "SMTPAuthenticationError: (454, b'cannot authenticate')",
                       "(454, b'cannot authenticate')",
                       "SMTPRecipientsRefused: {'recipient@example.com': (450, b'busy')}",
                       self.reason + "\n550 permanent failure"):
            self.assertFalse(temporary_smtp_rejection(reason), reason)

    @mute_logger('odoo.addons.mail.models.mail_mail')
    def test_real_send_failure_schedules_retry_and_success_cancels_it(self):
        mail = self._mail()
        with patch.object(type(self.Server), 'send_email', side_effect=SMTPDataError(451, b'4.3.0 try later')):
            mail._send()
        self.assertEqual(mail.state, 'exception')
        self.assertTrue(mail.operations_retry_next_at)
        self.assertEqual(mail.operations_retry_count, 0)
        self.assertIn('451', mail.operations_last_smtp_error)
        mail.operations_retry_next_at = self.now - timedelta(seconds=1)
        self.Mail._cron_retry_temporary_failures()
        with patch.object(type(self.Server), 'send_email', return_value='<test-smtp-accepted@example.com>'):
            mail._send()
        self.assertEqual(mail.state, 'sent')
        self.assertFalse(mail.operations_retry_next_at)
        self.assertEqual(mail.operations_retry_note, 'Sent')
        self.assertEqual(self.Mail._cron_retry_temporary_failures(), 0)

    def test_retry_backoff_cap_and_idempotent_queue_reservation(self):
        mail = self._mail()
        for index, delay in enumerate(RETRY_MINUTES):
            before = fields.Datetime.now()
            self._failure(mail)
            self.assertGreaterEqual(mail.operations_retry_next_at, before + timedelta(minutes=delay))
            self.assertLessEqual(mail.operations_retry_next_at,
                                 fields.Datetime.now() + timedelta(minutes=delay))
            self.assertEqual(self.Mail._cron_retry_temporary_failures(), 0)
            mail.operations_retry_next_at = self.now - timedelta(seconds=1)
            with patch.object(type(self.env['ir.cron']), '_trigger') as trigger:
                self.assertEqual(self.Mail._cron_retry_temporary_failures(), 1)
                self.assertEqual(self.Mail._cron_retry_temporary_failures(), 0)
                self.assertEqual(trigger.call_count, 1)
            self.assertEqual(mail.state, 'outgoing')
            self.assertEqual(mail.operations_retry_count, index + 1)
        self._failure(mail)
        self.assertFalse(mail.operations_retry_next_at)
        self.assertIn('exhausted', mail.operations_retry_note)

    @mute_logger('odoo.addons.mail.models.mail_mail')
    def test_permanent_and_authentication_failures_stay_failed(self):
        for error in (SMTPDataError(550, b'unknown recipient'), SMTPAuthenticationError(454, b'cannot authenticate')):
            mail = self._mail()
            with patch.object(type(self.Server), 'send_email', side_effect=error):
                mail._send()
            self.assertEqual(mail.state, 'exception')
            self.assertFalse(mail.operations_retry_next_at)
            self.assertIn('Manual review', mail.operations_retry_note)

    def test_partial_delivery_is_not_automatically_replayed(self):
        partner = self.env['res.partner'].create({'name': 'Already accepted', 'email': 'accepted@example.com'})
        mail = self._mail()
        self._failure(mail, success_pids=[partner])
        self.assertFalse(mail.operations_retry_next_at)
        self.assertIn('Partial delivery', mail.operations_retry_note)

    @mute_logger('odoo.addons.mail.models.mail_mail')
    def test_real_partial_and_mixed_recipient_sends_are_not_replayed(self):
        partners = self.env['res.partner'].create([
            {'name': 'First recipient', 'email': 'first@example.com'},
            {'name': 'Second recipient', 'email': 'second@example.com'},
        ])
        for email_to, recipient_ids in ((False, partners.ids), ('generic@example.com', partners[:1].ids)):
            mail = self._mail(email_to=email_to, recipient_ids=[(6, 0, recipient_ids)])
            with patch.object(type(self.Server), 'send_email', side_effect=[
                '<first-smtp-accepted@example.com>', SMTPDataError(451, b'try later'),
            ]) as send:
                mail._send()
            self.assertEqual(send.call_count, 2)
            self.assertEqual(mail.state, 'exception')
            self.assertFalse(mail.operations_retry_next_at)
            self.assertIn('review recipients', mail.operations_retry_note)

    @mute_logger('odoo.addons.mail.models.mail_mail')
    def test_connection_failure_is_retried_but_authentication_is_not(self):
        for error, retryable in ((SMTPDataError(421, b'try later'), True),
                                 (SMTPAuthenticationError(454, b'cannot authenticate'), False)):
            mail = self._mail()
            with patch.object(type(self.Server), 'connect', side_effect=error):
                mail.send()
            self.assertEqual(mail.state, 'exception')
            self.assertEqual(bool(mail.operations_retry_next_at), retryable)

    def test_cancelled_and_historical_failures_are_not_replayed(self):
        old = self._mail(state='exception', failure_reason=self.reason)
        canceled = self._mail()
        self._failure(canceled)
        canceled.write({'state': 'cancel', 'operations_retry_next_at': self.now - timedelta(seconds=1)})
        self.assertEqual(self.Mail._cron_retry_temporary_failures(), 0)
        self.assertEqual(old.state, 'exception')
        self.assertEqual(canceled.state, 'cancel')

    @mute_logger('odoo.addons.mail.models.mail_mail')
    def test_autodelete_success_updates_notification_without_resending(self):
        partner = self.env['res.partner'].create({'name': 'Retry recipient', 'email': 'retry@example.com'})
        message = self.env['mail.message'].create({'body': '<p>Test</p>', 'message_type': 'notification'})
        mail = self._mail(mail_message_id=message.id, is_notification=True, auto_delete=True,
                          email_to=False, recipient_ids=[(6, 0, partner.ids)])
        notification = self.env['mail.notification'].create({
            'mail_message_id': message.id, 'mail_mail_id': mail.id, 'res_partner_id': partner.id,
            'notification_type': 'email', 'notification_status': 'ready',
        })
        with patch.object(type(self.Server), 'send_email', side_effect=SMTPDataError(451, b'try later')):
            mail._send()
        self.assertTrue(mail.exists())
        self.assertEqual(notification.notification_status, 'exception')
        mail.operations_retry_next_at = self.now - timedelta(seconds=1)
        self.Mail._cron_retry_temporary_failures()
        with patch.object(type(self.Server), 'send_email', return_value='<retry-success@example.com>') as send:
            mail._send()
        self.assertEqual(send.call_count, 1)
        self.assertFalse(mail.exists())
        self.assertEqual(notification.notification_status, 'sent')
        self.assertFalse(notification.failure_reason)

    def test_monitor_snapshots_error_and_shows_retry_without_email_disclosure(self):
        mail = self._mail()
        self._failure(mail)
        observations = self.monitor._probe_mail(self.now)
        failure = next(item for item in observations if item['code'] == 'mail:failed')
        incident = self.monitor._reconcile([failure], {'mail'}, self.now)
        self.assertEqual(incident.source_mail_error, self.reason)
        self.assertEqual(incident.source_mail_url, '/odoo/mail.mail/%s' % mail.id)
        self.assertIn('retry 1 of 3 scheduled', incident.source_mail_status)
        self.assertEqual(incident.source_mail_retry_at, mail.operations_retry_next_at)
        self.assertNotIn('SMTPDataError', self.monitor._email_body(incident, self.now))
        self.assertNotIn('451', str(self.monitor._health_payload('not-a-token', self.now)))
        mail.unlink()
        incident.invalidate_recordset()
        self.assertEqual(incident.source_mail_error, self.reason)
        self.assertFalse(incident.source_mail_url)
        self.assertIn('no longer available', incident.source_mail_status)
        self.monitor._reconcile([], {'mail'}, self.now)
        self.assertEqual(incident.state, 'open')

    def test_existing_incident_derives_mail_id_and_keeps_error_as_plain_text(self):
        mail = self._mail(state='exception', failure_reason='<script>alert(1)</script>\nSMTPDataError: (451, busy)')
        incident = self.env['tradeline.operations.incident'].sudo().create({
            'monitor_id': self.monitor.id, 'code': 'mail:failed', 'title': 'Email failed',
            'severity': 'critical', 'evidence_version': '%s:old-timestamp' % mail.id,
        })
        self.assertIn('<script>', incident.source_mail_error)  # Text field, never rendered as HTML.
        self.assertEqual(incident.source_mail_url, '/odoo/mail.mail/%s' % mail.id)
        self.assertIn('Manual review', incident.source_mail_status)
        incident.evidence_version = '9' * 5000
        self.assertFalse(incident.source_mail_error)
        self.assertFalse(incident.source_mail_url)

    def test_alert_sender_respects_future_schedule(self):
        self.monitor.recipient_emails = 'operator@example.com'
        mail = self.monitor._queue_email('Scheduled test', 'Test')
        mail.scheduled_date = self.now + timedelta(hours=1)
        with patch.object(type(mail), 'send') as send:
            self.monitor._cron_send_alerts()
        send.assert_not_called()

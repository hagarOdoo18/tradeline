import json
import base64
import hashlib
from asn1crypto import cms
from contextlib import ExitStack
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user

from ..models.client import EtaClient, EtaError
from ..models.eta_monitoring import error_details


@tagged('post_install', '-at_install')
class TestEtaReliability(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.env.ref('base.EGP').active = True
        cls.company.write({'client_id': 'test-client', 'client_secret': 'test-secret',
                           'config_type': 'uat', 'config_version': '1.0', 'signature_tool': 'python'})
        Account = cls.env['account.account']
        cls.receivable = Account.create({'name': 'ETA test receivable', 'code': 'ETARCV',
            'account_type': 'asset_receivable', 'reconcile': True, 'company_ids': [Command.set(cls.company.ids)]})
        cls.income = Account.create({'name': 'ETA test income', 'code': 'ETAINC',
            'account_type': 'income', 'company_ids': [Command.set(cls.company.ids)]})
        cls.partner = cls.env['res.partner'].create({'name': 'ETA test buyer',
            'property_account_receivable_id': cls.receivable.id})
        cls.journal = cls.env['account.journal'].create({'name': 'ETA test', 'code': 'ETAT',
            'type': 'sale', 'company_id': cls.company.id, 'edi_format_ids': [Command.clear()]})

    def invoice(self, invoice_date=None):
        move = self.env['account.move'].create({'move_type': 'out_invoice', 'journal_id': self.journal.id,
            'currency_id': self.env.ref('base.EGP').id,
            'partner_id': self.partner.id, 'invoice_date': invoice_date or fields.Date.today(),
            'invoice_line_ids': [Command.create({'name': 'Test item', 'account_id': self.income.id,
                'quantity': 1, 'price_unit': 100, 'tax_ids': [Command.clear()]})]})
        move.action_post()
        return move

    def test_posting_queues_without_http(self):
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Posting performed HTTP')):
            move = self.invoice()
        self.assertEqual(len(move.eta_job_ids), 1)
        self.assertEqual(move.eta_job_ids.state, 'queued')

    def _id_rule_partner(self, **values):
        details = dict(company_type='person', mobile_type='local', vat=False,
                       display_name='ID rule test buyer', country_id=SimpleNamespace(code='EG'),
                       state_id=SimpleNamespace(display_name='Cairo'), city='Cairo', street='Test street',
                       street2=False, zip=False, floor=False, room=False, landmark=False, additional_info=False)
        details.update(values)
        return SimpleNamespace(**details)

    def test_missing_national_id_is_optional_below_25000(self):
        partner = self._id_rule_partner()
        for total in (0, 24999.99):
            result = self.env['account.move']._get_eta_personal_details(partner, total)
            self.assertEqual(result['type'], 'P')
            self.assertEqual(result['id'], '')
            self.assertFalse(partner.vat)

    def test_national_id_is_required_at_inclusive_25000_boundary(self):
        for total in (25000, 49999, 50000, 50001, -25000):
            with self.assertRaisesRegex(UserError, '25,000'):
                self.env['account.move']._get_eta_personal_details(self._id_rule_partner(), total)

    def test_required_national_id_must_have_fourteen_digits(self):
        for national_id in ('1234567890123', '1234567890123A', '123456789012345'):
            with self.assertRaisesRegex(UserError, '14-digit'):
                self.env['account.move']._get_eta_personal_details(self._id_rule_partner(vat=national_id), 25000)
        partner = self._id_rule_partner(vat='12345678901234')
        self.assertEqual(self.env['account.move']._get_eta_personal_details(partner, 25000)['id'], partner.vat)

    def test_optional_recorded_id_is_not_removed_below_threshold(self):
        partner = self._id_rule_partner(vat='12345678901234')
        self.assertEqual(self.env['account.move']._get_eta_personal_details(partner, 100)['id'], partner.vat)

    def test_optional_placeholder_ids_are_omitted_without_editing_customer(self):
        for value in ('1', '00000000000000', '11111111111111', '1234567890123A'):
            partner = self._id_rule_partner(vat=value)
            for amount in (350, 1250, -3198, 24999.99):
                details = self.env['account.move']._get_eta_personal_details(partner, amount)
                self.assertEqual((details['type'], details['id']), ('P', ''))
                self.assertEqual(partner.vat, value)
            kept = self.env['account.move'].with_context(eta_keep_recorded_person_id=True)._get_eta_personal_details(partner, 350)
            self.assertEqual(kept['id'], value)
        with self.assertRaisesRegex(UserError, '14-digit'):
            self.env['account.move']._get_eta_personal_details(self._id_rule_partner(vat='1'), 25000)

    def test_optional_id_policy_preserves_recorded_reference_and_amounts(self):
        move = self.invoice(); partner = move.partner_id
        partner.with_context(no_vat_validation=True).write({'vat': '12345678901234', 'country_id': self.env.ref('base.eg').id})
        move.eta_job_ids.omit_optional_receiver_id = True
        before = (move.invoice_date, move.amount_total, partner.vat)
        with patch.object(type(partner), 'mobile_type', 'local', create=True):
            self.assertEqual(move._get_eta_personal_details(partner, 100)['id'], '')
            self.assertEqual(move.with_context(eta_keep_recorded_person_id=True)._get_eta_personal_details(partner, 100)['id'], partner.vat)
        self.assertEqual(before, (move.invoice_date, move.amount_total, partner.vat))

    def test_recorded_rejections_in_review_are_visible_without_communications_alarm(self):
        job = self.invoice().eta_job_ids; jobs = self.env['tradeline.eta.job']
        monitor = self.env['tradeline.operations.monitor'].with_context(active_test=False).search([], limit=1)
        monitor = monitor or self.env['tradeline.operations.monitor'].create({})
        self.env.ref('tradeline_eta_reliability.cron_eta_queue').write({'active': True, 'nextcall': fields.Datetime.now() + timedelta(minutes=1)})
        self.env['ir.config_parameter'].sudo().set_param('tradeline_eta_reliability.enabled_database', self.env.cr.dbname)
        self.env['ir.config_parameter'].sudo().set_param('tradeline_eta_reliability.last_processed_at', str(fields.Datetime.now()))
        messages = [
            (True, 'ETA rejected the submission (1). ' + json.dumps({'code': '1', 'details': [{'code': 'CF305', 'message': 'Receiver ID required'}]})),
            (False, json.dumps({'status': 'Invalid', 'validationSteps': [{'status': 'Invalid', 'error': {'errorCode': 'CV302', 'error': 'Item code does not exist'}}]})),
        ]
        for safe, message in messages:
            job.write({'state': 'review', 'safe_to_retry': safe, 'failures': 0, 'last_error': message})
            self.assertTrue(job.needs_attention)
            self.assertIn(job, jobs.search([('needs_attention', '=', True)]))
            self.assertIn('previous submission was rejected', job.required_action)
            self.assertTrue('customer ID' in job.required_action or 'product details' in job.required_action)
            with patch.object(type(jobs), 'search_count', side_effect=lambda domain, **kw: len(job.filtered_domain(domain))):
                codes = [s['code'] for s in monitor._probe_crons(fields.Datetime.now())]
            self.assertIn('eta:rejected', codes); self.assertNotIn('eta:stalled', codes)
        job.write({'safe_to_retry': False, 'last_error': 'Automatic authority-history scan is in progress.'})
        self.assertFalse(job.needs_attention)
        self.assertNotIn(job, jobs.search([('needs_attention', '=', True)]))

    def test_national_id_rule_keeps_foreign_and_business_identity_rules(self):
        foreign = self._id_rule_partner(mobile_type='foreigner', vat='TEST-PASSPORT',
                                        country_id=SimpleNamespace(code='US'))
        result = self.env['account.move']._get_eta_personal_details(foreign, 60000)
        self.assertEqual((result['type'], result['id']), ('F', 'TEST-PASSPORT'))
        with self.assertRaisesRegex(UserError, 'Tax ID'):
            self.env['account.move']._get_eta_personal_details(self._id_rule_partner(company_type='company'), 100)

    def test_national_id_threshold_uses_posted_total_after_discount(self):
        move = self.invoice()
        # Legacy order_total was the gross line price before discounts.
        partner = move.partner_id
        with patch.object(type(partner), 'mobile_type', 'local', create=True), \
                patch.object(type(self.env['res.currency']), '_convert', return_value=100):
            result = move._get_eta_personal_details(partner, 60000)
        self.assertEqual(result['type'], 'P')
        self.assertEqual(result['id'], '')

    def test_historical_backlog_does_not_starve_new_submissions_or_statuses(self):
        for state in ('queued', 'accepted', 'unknown'):
            self.invoice().eta_job_ids.write({'state': state, 'next_attempt': False})
        due = self.env['tradeline.eta.job']._due_jobs('all')
        self.assertEqual([job.state for job in due[:3]], ['queued', 'accepted', 'unknown'])

    def test_fresh_and_old_queued_invoices_both_progress_during_recovery(self):
        self.env['tradeline.eta.job'].sudo().search([('state', '=', 'queued')]).write({
            'next_attempt': fields.Datetime.now() + timedelta(days=1)})
        older = self.invoice().eta_job_ids
        fresh = self.invoice().eta_job_ids
        self.env.cr.execute('UPDATE tradeline_eta_job SET create_date=%s WHERE id=%s',
                            [fields.Datetime.now() - timedelta(days=2), older.id])
        due = self.env['tradeline.eta.job']._due_jobs('submit')
        self.assertEqual([job for job in due if job.state == 'queued'][:2], [fresh, older])
        self.assertEqual(sum(job.id == fresh.id for job in due), 1)
        self.assertEqual(sum(job.id == older.id for job in due), 1)

    def test_monitor_detects_wrong_database_activation(self):
        monitor = self.env['tradeline.operations.monitor'].with_context(active_test=False).search([], limit=1)
        if not monitor:
            monitor = self.env['tradeline.operations.monitor'].create({})
        self.env.ref('tradeline_eta_reliability.cron_eta_queue').active = True
        self.env['ir.config_parameter'].sudo().set_param('tradeline_eta_reliability.enabled_database', 'different-database')
        self.assertIn('eta:scheduler', [s['code'] for s in monitor._probe_crons(fields.Datetime.now())])

    def test_monitor_detects_a_queue_that_never_started(self):
        job = self.invoice().eta_job_ids
        self.env.cr.execute('UPDATE tradeline_eta_job SET create_date=%s WHERE id=%s',
                            [fields.Datetime.now() - timedelta(minutes=30), job.id])
        monitor = self.env['tradeline.operations.monitor'].with_context(active_test=False).search([], limit=1)
        if not monitor:
            monitor = self.env['tradeline.operations.monitor'].create({})
        self.assertEqual(job.failures, 0)
        self.assertIn('eta:stalled', [s['code'] for s in monitor._probe_crons(fields.Datetime.now())])

    def test_eta_alert_resolves_after_scheduler_recovers_but_not_after_failed_probe(self):
        monitor = self.env['tradeline.operations.monitor'].with_context(active_test=False).search([], limit=1)
        if not monitor:
            monitor = self.env['tradeline.operations.monitor'].create({})
        now = fields.Datetime.now()
        self.env.ref('tradeline_eta_reliability.cron_eta_queue').active = True
        self.env['ir.config_parameter'].sudo().set_param(
            'tradeline_eta_reliability.enabled_database', 'different-database')
        observations = [s for s in monitor._probe_crons(now) if s['code'] == 'eta:scheduler']
        monitor._reconcile(observations, {'crons'}, now)
        incident = monitor.incident_ids.filtered(lambda i: i.code == 'eta:scheduler')
        self.assertTrue(incident)
        monitor._reconcile([], set(), now)
        self.assertNotEqual(incident.state, 'resolved')
        self.env['ir.config_parameter'].sudo().set_param(
            'tradeline_eta_reliability.enabled_database', self.env.cr.dbname)
        self.env.ref('tradeline_eta_reliability.cron_eta_queue').write({'nextcall': now, 'failure_count': 0})
        self.assertFalse([s for s in monitor._probe_crons(now) if s['code'] == 'eta:scheduler'])
        monitor._reconcile([], {'crons'}, now)
        self.assertEqual(incident.state, 'resolved')

    def test_signer_does_not_commit_and_has_timeout(self):
        move = self.invoice()
        move.e_invoice_canonical = 'canonical'
        with patch('odoo.addons.tradeline_eta_reliability.models.account_move.requests.get',
                   return_value=SimpleNamespace(status_code=200, text='signed')) as http, \
                patch.object(type(move), '_eta_validate_signature_digest', return_value=True):
            with patch.object(type(self.env.cr), 'commit', side_effect=AssertionError('Signer committed')):
                self.assertEqual(move.get_signature_value(), 'signed')
        self.assertEqual(http.call_args.kwargs['timeout'], (5, 25))

    def test_known_uuid_is_never_resubmitted(self):
        move = self.invoice()
        move.write({'e_invoice_uuid': 'known-uuid', 'e_invoice_status': 'Submitted'})
        client = MagicMock()
        client.request.return_value = {'uuid': 'known-uuid', 'internalId': move.name, 'status': 'Valid'}
        move.eta_job_ids._submit(client)
        self.assertEqual(client.request.call_args.args[0], 'GET')
        self.assertEqual(move.eta_job_ids.state, 'valid')

    def test_uncertain_submission_is_not_resent(self):
        move = self.invoice()
        job = move.eta_job_ids
        job.write({'state': 'unknown', 'attempted_at': fields.Datetime.now(),
                   'payload': json.dumps({'documents': [{'internalID': move.name, 'dateTimeIssued': str(move.invoice_date) + 'T00:00:00Z'}]})})
        client = MagicMock()
        client.request.return_value = {'result': [], 'metadata': {'continuationToken': 'EndofResultSet'}}
        job._refresh(client)
        self.assertEqual(job.state, 'unknown')
        self.assertTrue(all(c.args[0] == 'GET' for c in client.request.call_args_list))
        self.assertFalse(move.e_invoice_uuid)

    def test_unattempted_legacy_document_requires_authoritative_absence(self):
        move = self.invoice()
        job = move.eta_job_ids
        job.write({'state': 'unknown', 'first_submission_candidate': True})
        client = MagicMock()
        client.request.return_value = {'result': [], 'metadata': {'continuationToken': 'EndofResultSet'}}
        job._refresh(client)
        self.assertEqual(job.state, 'queued')
        self.assertTrue(all(c.args[0] == 'GET' for c in client.request.call_args_list))

    def test_lost_response_is_not_treated_as_an_unattempted_document(self):
        move = self.invoice()
        job = move.eta_job_ids
        job.write({'state': 'unknown', 'first_submission_candidate': True,
                   'attempted_at': fields.Datetime.now()})
        client = MagicMock()
        client.request.return_value = {'result': [], 'metadata': {'continuationToken': 'EndofResultSet'}}
        job._refresh(client)
        self.assertEqual(job.state, 'unknown')

    def test_unknown_does_not_match_reused_internal_id(self):
        move = self.invoice()
        job = move.eta_job_ids
        job.write({'state': 'unknown', 'attempted_at': fields.Datetime.now(),
                   'payload': json.dumps({'documents': [{'internalID': move.name, 'totalAmount': 100}]})})
        client = MagicMock()
        client.request.side_effect = [
            {'result': [{'uuid': 'other', 'internalId': move.name}]},
            {'uuid': 'other', 'internalId': move.name, 'status': 'Valid',
             'document': json.dumps({'internalID': move.name, 'totalAmount': 200})}]
        job._refresh(client)
        self.assertEqual(job.state, 'unknown')
        self.assertFalse(move.e_invoice_uuid)

    def test_wrong_identity_cannot_mark_valid(self):
        move = self.invoice()
        with self.assertRaises(EtaError):
            move.eta_job_ids._apply_remote({'uuid': 'wrong', 'internalId': 'different-invoice', 'status': 'Valid'})
        self.assertFalse(move.e_invoice_uuid)

    def test_native_dispatcher_does_not_send_a_second_copy(self):
        move = self.invoice()
        eta = self.env.ref('l10n_eg_edi_eta.edi_eg_eta')
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Native dispatcher sent HTTP')):
            result = eta._l10n_eg_edi_post_invoice(move)
        self.assertEqual(result[move]['blocking_level'], 'info')
        self.assertEqual(len(move.eta_job_ids), 1)

    def test_verified_valid_status_clears_only_matching_native_error(self):
        move = self.invoice()
        eta = self.env.ref('l10n_eg_edi_eta.edi_eg_eta')
        native = self.env['account.edi.document'].create({'move_id': move.id, 'edi_format_id': eta.id,
            'state': 'to_send', 'error': 'Missing native signing payload', 'blocking_level': 'error'})
        move.eta_job_ids._apply_remote({'uuid': 'verified-uuid', 'internalId': move.name, 'status': 'Valid',
            'submissionUUID': 'verified-submission', 'document': json.dumps({'internalID': move.name, 'dateTimeIssued': str(move.invoice_date) + 'T00:00:00Z'})})
        self.assertEqual(native.state, 'sent')
        self.assertFalse(native.error)
        self.assertEqual(move.l10n_eg_uuid, 'verified-uuid')

    def test_invalid_remains_visible_and_not_queued(self):
        move = self.invoice()
        move.eta_job_ids._apply_remote({'uuid': 'eta-uuid', 'internalId': move.name, 'status': 'Invalid'})
        self.assertEqual(move.e_invoice_status, 'Invalid')
        self.assertEqual(move.eta_job_ids.state, 'invalid')
        self.assertTrue(move.eta_job_ids.last_error)

    def test_async_rejection_fetches_field_details_before_becoming_terminal(self):
        move = self.invoice()
        move.e_invoice_uuid = 'eta-invalid'
        job = move.eta_job_ids
        job.submission_uuid = 'eta-submission'
        client = MagicMock()
        client.request.side_effect = [
            {'documentSummary': [{'uuid': 'eta-invalid', 'internalId': move.name, 'status': 'Invalid'}]},
            {'uuid': 'eta-invalid', 'internalId': move.name, 'status': 'Invalid',
             'validationResults': {'errorCode': 'NI310', 'propertyName': 'receiverId', 'error': 'Invalid customer ID'}}]
        job._refresh(client)
        self.assertEqual(job.state, 'invalid')
        self.assertIn('NI310', job.last_error)
        self.assertIn('receiverId', move.e_invoice_issue)
        self.assertEqual([c.args[0] for c in client.request.call_args_list], ['GET', 'GET'])

    def test_old_invoice_is_blocked_without_rewriting_date(self):
        old = fields.Date.today() - timedelta(days=30)
        move = self.invoice(invoice_date=old)
        with self.assertRaises(UserError):
            move.eta_job_ids._prepare()
        self.assertEqual(move.invoice_date, old)

    def test_generated_document_and_canonical_use_invoice_date_instead_of_submission_day(self):
        issued = date(2026, 7, 15)
        move = self.invoice(invoice_date=issued)
        with patch.object(type(move), '_get_eta_invoice_lines', return_value=([], 0, 100, 100)), \
                patch.object(type(move), '_get_eta_personal_details', side_effect=lambda *a: {'address': {}}), \
                patch.object(type(move), '_get_eta_tax_totals', return_value=[]), \
                patch.object(type(move), 'inv_type', '', create=True), \
                patch.object(type(move), 'reference_number', '', create=True), \
                patch('requests.sessions.Session.request', side_effect=AssertionError('Generation performed HTTP')):
            document, _environment = move.action_generate_eta_json()
        expected = '2026-07-14T21:00:00Z'
        self.assertEqual(document['dateTimeIssued'], expected)
        self.assertIn('"DATETIMEISSUED""%s"' % expected, move.e_invoice_canonical)
        self.assertEqual(move.invoice_date, issued)

    def test_cairo_issue_dates_cover_winter_summer_and_clock_changes(self):
        for issued, expected in (
                (date(2026, 1, 15), '2026-01-14T22:00:00Z'),
                (date(2026, 7, 15), '2026-07-14T21:00:00Z'),
                (date(2026, 4, 24), '2026-04-23T22:00:00Z'),
                (date(2026, 10, 30), '2026-10-29T22:00:00Z')):
            move = self.invoice(invoice_date=issued)
            # Exercise the midnight fallback even when the test runs on one
            # of these dates; matching stored issue-time evidence is separate.
            self.env.cr.execute('UPDATE account_move SET create_date=%s WHERE id=%s',
                                [fields.Datetime.to_datetime('2025-01-01 00:00:00'), move.id])
            move.invalidate_recordset(['create_date'])
            self.assertEqual(move._eta_issue_timestamp(), expected)

    def test_valid_cairo_date_can_have_previous_utc_date(self):
        move = self.invoice(invoice_date=date(2026, 7, 15))
        job = move.eta_job_ids
        job.payload = json.dumps({'documents': [{'internalID': move.name,
            'dateTimeIssued': '2026-07-14T21:00:00Z'}]})
        client = MagicMock()
        client.request.return_value = {'acceptedDocuments': [{'uuid': 'cairo-date', 'internalId': move.name}]}
        with patch.object(self.env.cr, 'commit'):
            job._submit(client)
        self.assertEqual(job.state, 'accepted')
        self.assertEqual(client.request.call_count, 1)

    def test_stored_creation_time_is_retained_only_on_the_invoice_cairo_date(self):
        move = self.invoice(invoice_date=date(2026, 7, 15))
        for created, expected in (
                ('2026-07-14 21:30:00', '2026-07-14T21:30:00Z'),
                ('2026-07-15 18:45:00', '2026-07-15T18:45:00Z'),
                ('2026-07-16 12:00:00', '2026-07-14T21:00:00Z')):
            self.env.cr.execute('UPDATE account_move SET create_date=%s WHERE id=%s', [created, move.id])
            move.invalidate_recordset(['create_date'])
            self.assertEqual(move._eta_issue_timestamp(), expected)

    def test_previously_prepared_wrong_issue_date_cannot_be_transmitted(self):
        move = self.invoice(invoice_date=fields.Date.today() - timedelta(days=1))
        job = move.eta_job_ids
        job.payload = json.dumps({'documents': [{'internalID': move.name,
            'dateTimeIssued': str(fields.Date.today()) + 'T00:00:00Z'}]})
        client = MagicMock()
        with self.assertRaises(UserError):
            job._submit(client)
        client.request.assert_not_called()
        self.assertFalse(job.attempted_at)
        self.assertEqual(job.attempts, 0)

    def test_ambiguous_post_marker_survives_transport_failure(self):
        move = self.invoice()
        job = move.eta_job_ids
        job.payload = json.dumps({'documents': [{'internalID': move.name, 'dateTimeIssued': str(move.invoice_date) + 'T00:00:00Z'}]})
        client = MagicMock()
        client.request.side_effect = requests.Timeout()
        with patch.object(self.env.cr, 'commit') as commit:
            try:
                job._submit(client)
            except requests.Timeout:
                pass
            else:
                self.fail('The simulated HTTP timeout did not occur')
        self.assertTrue(commit.called)
        self.assertEqual(job.state, 'unknown')
        self.assertTrue(job.attempted_at)

    def test_throttled_post_can_retry_without_being_lost(self):
        move = self.invoice()
        job = move.eta_job_ids
        job.payload = json.dumps({'documents': [{'internalID': move.name, 'dateTimeIssued': str(move.invoice_date) + 'T00:00:00Z'}]})
        client = MagicMock()
        client.request.side_effect = EtaError('ETA returned HTTP 429.', 429, 120)
        with patch.object(self.env.cr, 'commit'):
            job._submit(client)
        self.assertEqual(job.state, 'queued')
        self.assertTrue(job.next_attempt)
        self.assertEqual(job.failures, 1)

    def test_accepted_post_is_saved_with_uuid(self):
        move = self.invoice()
        job = move.eta_job_ids
        job.payload = json.dumps({'documents': [{'internalID': move.name, 'dateTimeIssued': str(move.invoice_date) + 'T00:00:00Z'}]})
        client = MagicMock()
        client.request.return_value = {'submissionUUID': 'submission-1',
            'acceptedDocuments': [{'uuid': 'accepted-1', 'internalId': move.name, 'longId': 'long-1'}],
            'rejectedDocuments': []}
        with patch.object(self.env.cr, 'commit'):
            job._submit(client)
        self.assertEqual(job.state, 'accepted')
        self.assertEqual(move.e_invoice_uuid, 'accepted-1')
        self.assertEqual(job.submission_uuid, 'submission-1')
        self.assertFalse(move.eta_submission_pending)
        self.assertEqual(move.e_invoice_date, job.attempted_at)

    def test_submission_validation_details_remain_actionable_without_automatic_resend(self):
        move = self.invoice()
        job = move.eta_job_ids
        job.payload = json.dumps({'documents': [{'internalID': move.name, 'dateTimeIssued': str(move.invoice_date) + 'T00:00:00Z'}]})
        client = MagicMock()
        client.request.return_value = {'acceptedDocuments': [], 'rejectedDocuments': [{
            'internalId': move.name, 'error': {'code': '1', 'message': 'Validation failed',
                'details': [{'target': 'itemCode', 'message': 'Code is not approved'}]}}]}
        with patch.object(self.env.cr, 'commit'):
            job._submit(client)
        self.assertEqual(job.state, 'blocked')
        self.assertIn('itemCode', job.last_error)
        self.assertIn('Code is not approved', move.e_invoice_issue)
        self.assertNotIn(job, self.env['tradeline.eta.job']._due_jobs('all'))
        self.assertEqual(move.amount_total, 100)

    def test_corrected_invalid_document_queues_without_posting_or_losing_history(self):
        move = self.invoice()
        move.write({'e_invoice_uuid': 'invalid-old', 'e_invoice_status': 'Invalid', 'e_invoice_sent': True})
        client = MagicMock()
        client.request.return_value = {'uuid': 'invalid-old', 'internalId': move.name, 'status': 'Invalid',
            'document': {'internalID': move.name, 'receiver': {'id': 'old-id'}, 'totalAmount': 100}}
        corrected = {'internalID': move.name, 'receiver': {'id': 'corrected-id'}, 'totalAmount': 100}
        with patch('odoo.addons.tradeline_eta_reliability.models.account_move.EtaClient', return_value=client):
            with patch.object(type(move), '_eta_correction_document', return_value=corrected):
                move.action_send_electronic_invoice()
        self.assertFalse(move.e_invoice_uuid)
        self.assertEqual(move.eta_job_ids.state, 'queued')
        self.assertTrue(all(c.args[0] == 'GET' for c in client.request.call_args_list))
        self.assertTrue(self.env['ir.attachment'].search_count([
            ('res_model', '=', 'account.move'), ('res_id', '=', move.id), ('name', '=', 'ETA_rejected_invalid-old.json')]))
        self.assertEqual(move.amount_total, 100)

    def test_unchanged_invalid_document_cannot_be_resent(self):
        move = self.invoice()
        move.write({'e_invoice_uuid': 'invalid-old', 'e_invoice_status': 'Invalid', 'e_invoice_sent': True})
        client = MagicMock()
        original = {'internalID': move.name, 'totalAmount': 100, 'dateTimeIssued': '2026-01-01T00:00:00Z'}
        client.request.return_value = {'uuid': 'invalid-old', 'internalId': move.name, 'status': 'Invalid', 'document': original}
        with patch('odoo.addons.tradeline_eta_reliability.models.account_move.EtaClient', return_value=client):
            with patch.object(type(move), '_eta_correction_document', return_value=dict(original, dateTimeIssued='2026-10-28T00:00:00Z')):
                with self.assertRaises(UserError):
                    move.action_send_electronic_invoice()
        self.assertEqual(move.e_invoice_uuid, 'invalid-old')
        self.assertTrue(all(c.args[0] == 'GET' for c in client.request.call_args_list))

    def test_duplicate_rejection_requires_original_receipt_reconciliation_before_correction(self):
        move = self.invoice()
        move.write({'e_invoice_uuid': 'duplicate-old', 'e_invoice_status': 'Invalid'})
        client = MagicMock()
        client.request.return_value = {'uuid': 'duplicate-old', 'internalId': move.name,
            'status': 'Invalid', 'validationResults': {'errorCode': 'DS301'},
            'document': {'internalID': move.name, 'receiver': {'id': 'old'}}}
        with patch('odoo.addons.tradeline_eta_reliability.models.account_move.EtaClient', return_value=client), \
                patch.object(type(move), '_eta_correction_document', return_value={
                    'internalID': move.name, 'receiver': {'id': 'changed'}}):
            with self.assertRaisesRegex(UserError, 'duplicate'):
                move.action_send_electronic_invoice()
        self.assertEqual(move.e_invoice_uuid, 'duplicate-old')
        self.assertEqual([c.args[0] for c in client.request.call_args_list], ['GET'])

    def test_previously_invalid_but_now_valid_document_is_refreshed_only(self):
        move = self.invoice()
        move.write({'e_invoice_uuid': 'same-uuid', 'e_invoice_status': 'Invalid'})
        client = MagicMock()
        client.request.return_value = {'uuid': 'same-uuid', 'internalId': move.name, 'status': 'Valid'}
        with patch('odoo.addons.tradeline_eta_reliability.models.account_move.EtaClient', return_value=client):
            move.action_send_electronic_invoice()
        self.assertEqual(move.e_invoice_uuid, 'same-uuid')
        self.assertEqual(move.e_invoice_status, 'Valid')
        self.assertTrue(all(c.args[0] == 'GET' for c in client.request.call_args_list))

    def test_timeout_and_throttling_are_bounded(self):
        client = EtaClient(self.company)
        client.token = 'test-only'
        client.token_until = float('inf')
        response = SimpleNamespace(ok=False, status_code=429, headers={'Retry-After': '120'})
        client.session.request = MagicMock(return_value=response)
        with self.assertRaises(EtaError) as caught:
            client.request('POST', '/api/v1/documentsubmissions', data=b'{}')
        self.assertEqual(caught.exception.retry_after, 121)
        self.assertEqual(client.session.request.call_count, 1)
        self.assertEqual(client.session.request.call_args.kwargs['timeout'], (5, 25))

    def test_invoice_log_explains_each_validation_leaf_without_http_or_writes(self):
        move = self.invoice()
        job = move.eta_job_ids
        job.write({'state': 'blocked', 'last_error': 'ETA rejected: ' + json.dumps({
            'error': {'code': 'ValidationError', 'message': 'Validation failed', 'details': [
                {'code': 'CF313', 'target': 'dateTimeIssued', 'message': 'Issue date outside permitted range'},
                {'code': 'CF305', 'target': 'receiver.id', 'message': 'Receiver ID not registered'}]}})})
        before = (move.invoice_date, move.amount_total, move.write_date, job.write_date, job.state)
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Log performed HTTP')):
            self.assertIn('CF313 / dateTimeIssued', job.reason_summary)
            self.assertIn('CF305 / receiver.id', job.reason_summary)
            self.assertNotIn('Validation failed', job.reason_summary)
            self.assertIn('Keep the original invoice date', job.required_action)
            self.assertIn('customer ID', job.required_action)
            self.assertTrue(job.needs_attention)
        self.assertEqual(before, (move.invoice_date, move.amount_total, move.write_date, job.write_date, job.state))

    def test_invoice_log_recovers_leaf_errors_from_truncated_json(self):
        raw = 'ETA rejected: {"error":{"code":"ValidationError","details":[' + json.dumps({
            'code': 'SF307', 'target': 'receiver.id', 'message': 'Receiver ID length invalid'}) + ',{"code":"CF313"'
        detail, codes = error_details(raw)
        self.assertIn('SF307 / receiver.id: Receiver ID length invalid', detail)
        self.assertEqual(codes, {'ValidationError', 'SF307', 'CF313'})
        job = self.invoice().eta_job_ids
        job.write({'state': 'invalid', 'last_error': raw})
        self.assertIn('customer ID', job.required_action)
        self.assertNotIn('tax calculation', job.required_action)
        self.assertIn('Keep the original invoice date', job.required_action)

    def test_invoice_log_attention_filters_match_live_states_and_attempt_age(self):
        jobs = self.env['tradeline.eta.job']
        queued, accepted, unknown, blocked, approved, reviewed = [self.invoice().eta_job_ids for _ in range(6)]
        now = fields.Datetime.now()
        old = now - timedelta(hours=2)
        all_jobs = queued | accepted | unknown | blocked | approved | reviewed
        self.env.cr.execute('UPDATE tradeline_eta_job SET create_date=%s WHERE id IN %s', [old, tuple(all_jobs.ids)])
        all_jobs.invalidate_recordset(['create_date'])
        accepted.write({'state': 'accepted', 'attempted_at': now})
        unknown.state = 'unknown'
        blocked.state = 'blocked'
        approved.state = 'valid'
        reviewed.write({'state': 'valid', 'last_error': 'ETA issue date differs from the original invoice date.'})
        expected = queued | unknown | blocked | reviewed
        base = [('id', 'in', all_jobs.ids)]
        self.assertEqual(set(jobs.search(base + [('needs_attention', '=', True)]).ids), set(expected.ids))
        self.assertEqual(set(jobs.search(base + [('needs_attention', '!=', False)]).ids), set(expected.ids))
        self.assertEqual(set(jobs.search(base + [('needs_attention', '=', False)]).ids), {accepted.id, approved.id})
        self.assertEqual(set(all_jobs.filtered('needs_attention').ids), set(expected.ids))
        self.assertLess(accepted.waiting_minutes, 2)
        accepted.attempted_at = now - timedelta(minutes=20)
        self.assertTrue(accepted.needs_attention)
        self.assertIn(accepted, jobs.search(base + [('needs_attention', '=', True)]))
        self.assertIn('Do not automatically cancel or resend', reviewed.required_action)

    def test_invoice_log_distinguishes_disabled_overdue_and_active_worker(self):
        job = self.invoice().eta_job_ids
        cron = self.env.ref('tradeline_eta_reliability.cron_eta_queue')
        self.env['ir.config_parameter'].sudo().set_param('tradeline_eta_reliability.enabled_database', self.env.cr.dbname)
        cron.write({'active': False, 'failure_count': 0})
        job._compute_monitoring()
        self.assertIn('disabled', job.reason_summary)
        job.last_error = 'ETA temporarily unavailable'
        self.assertIn('Restore the ETA scheduled worker', job.required_action)
        cron.write({'active': True, 'nextcall': fields.Datetime.now() - timedelta(minutes=10)})
        job.last_error = False
        job._compute_monitoring()
        self.assertIn('overdue', job.reason_summary)
        cron.nextcall = fields.Datetime.now() + timedelta(minutes=1)
        job._compute_monitoring()
        self.assertIn('Waiting in the automatic submission queue', job.reason_summary)

    def test_invoice_log_keeps_uncertain_outcomes_and_product_fixes_distinct(self):
        job = self.invoice().eta_job_ids
        job.write({'state': 'unknown', 'last_error': 'Read timed out after submission'})
        self.assertIn('Read timed out', job.reason_summary)
        self.assertIn('do not blindly resend', job.required_action)
        job.write({'state': 'invalid', 'last_error': 'CV302: itemCode is not active'})
        self.assertIn('ETA-approved EGS/GS1', job.required_action)
        job.write({'state': 'invalid', 'last_error': 'DS301: duplicate document'})
        self.assertIn('referenced original ETA receipt', job.required_action)

    def test_invoice_log_navigation_and_incident_drilldown(self):
        job = self.invoice().eta_job_ids
        self.assertEqual(job.action_open_invoice()['res_id'], job.move_id.id)
        monitor = self.env['tradeline.operations.monitor'].with_context(active_test=False).search([], limit=1)
        monitor = monitor or self.env['tradeline.operations.monitor'].create({})
        self.assertEqual(monitor.action_eta_invoice_log()['res_model'], 'tradeline.eta.job')
        for code, state in [('eta:rejected', 'blocked'), ('eta:stalled', 'unknown')]:
            incident = self.env['tradeline.operations.incident'].search([('monitor_id', '=', monitor.id), ('code', '=', code)])
            incident = incident or self.env['tradeline.operations.incident'].create({
                'monitor_id': monitor.id, 'code': code, 'title': 'ETA test incident',
                'severity': 'warning', 'details': 'Test', 'action': 'Review'})
            job.state = state
            self.assertTrue(incident.is_eta_incident)
            action = incident.action_eta_invoice_log()
            self.assertEqual(action['context'], {})
            self.assertIn(job, self.env['tradeline.eta.job'].search(action['domain']))

    def test_invoice_log_accounting_access_respects_selected_company(self):
        job = self.invoice().eta_job_ids
        partner_values = {'name': 'ETA log other company', 'is_company': True, 'vat': '999999999'}
        # The full hosted stack requires size/device data on company contacts.
        # These are rollback-only fixtures; no taxpayer document is submitted.
        for name, value in [('company_size', 'small'), ('company_device', 1)]:
            if name in self.env['res.partner']._fields:
                partner_values[name] = value
        company_partner = self.env['res.partner'].with_context(no_vat_validation=True).create(partner_values)
        company_b = self.env['res.company'].create({'name': 'ETA log other company',
            'partner_id': company_partner.id,
            'activity_code_id': self.company.activity_code_id.id,
            'type_code_id': self.company.type_code_id.id,
            'sub_type_code_id': self.company.sub_type_code_id.id})
        journal_b = self.env['account.journal'].with_company(company_b).create({
            'name': 'ETA log other sales', 'code': 'ELB', 'type': 'sale', 'company_id': company_b.id})
        move_b = self.env['account.move'].with_company(company_b).create({
            'move_type': 'out_invoice', 'journal_id': journal_b.id, 'company_id': company_b.id})
        other_job = self.env['tradeline.eta.job'].create({'move_id': move_b.id, 'state': 'blocked'})
        user = new_test_user(self.env, login='eta_log_manager', groups='account.group_account_manager',
            company_id=self.company.id, company_ids=[Command.set((self.company | company_b).ids)])
        visible = self.env['tradeline.eta.job'].with_user(user).with_context(allowed_company_ids=self.company.ids)
        self.assertEqual(visible.search([('id', 'in', (job | other_job).ids)]), job.with_user(user))
        self.assertTrue(visible.browse(job.id).reason_summary)
        with self.assertRaises(AccessError):
            visible.browse(other_job.id).action_open_invoice()
        with self.assertRaises(AccessError):
            self.env['tradeline.operations.monitor'].with_user(user).action_eta_invoice_log()

    def test_current_eta_date_approval_preserves_invoice_and_archives_previous_attempt(self):
        old = fields.Date.today() - timedelta(days=45)
        move = self.invoice(invoice_date=old)
        job = move.eta_job_ids
        previous = json.dumps({'documents': [{'dateTimeIssued': str(old) + 'T00:00:00Z'}]})
        job.write({'state': 'blocked', 'last_error': 'CF313: outside ETA window', 'payload': previous,
                   'payload_hash': 'previous-hash', 'attempted_at': fields.Datetime.now(), 'safe_to_retry': True})
        before = (move.invoice_date, move.amount_total, move.amount_tax, move.amount_untaxed, move.state)
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Approval performed HTTP')):
            job.action_approve_current_issue_date()
        self.assertEqual(job.state, 'queued')
        self.assertEqual(job.issue_date_policy, 'current')
        self.assertTrue(job.issue_date_approved_by and job.issue_date_approved_at and job.issue_date_approval_note)
        self.assertFalse(job.payload or job.attempted_at or job.current_issue_datetime)
        archive = self.env['ir.attachment'].search([('res_model', '=', 'account.move'), ('res_id', '=', move.id),
            ('name', '=', 'ETA_issue_date_approval_%s.json' % job.id)], limit=1)
        self.assertEqual(json.loads(archive.raw)['old_payload'], previous)
        self.assertEqual(archive.company_id, move.company_id)
        self.assertEqual(before, (move.invoice_date, move.amount_total, move.amount_tax, move.amount_untaxed, move.state))

    def test_current_eta_date_never_approves_known_or_uncertain_outcomes(self):
        for state in ('unknown', 'accepted', 'valid', 'invalid'):
            job = self.invoice().eta_job_ids
            job.state = state
            with self.assertRaises(UserError):
                job.action_approve_current_issue_date()
            self.assertEqual(job.issue_date_policy, 'invoice')
        for values in ({'e_invoice_uuid': 'known'}, {'e_invoice_sent': True}, {'l10n_eg_uuid': 'native-known'}):
            move = self.invoice()
            move.write(values)
            move.eta_job_ids.state = 'blocked'
            with self.assertRaises(UserError):
                move.eta_job_ids.action_approve_current_issue_date()

    def test_current_eta_date_cannot_clear_an_ambiguous_attempt(self):
        job = self.invoice().eta_job_ids
        job.write({'state': 'blocked', 'attempted_at': fields.Datetime.now(), 'safe_to_retry': False})
        with self.assertRaises(UserError):
            job.action_approve_current_issue_date()
        self.assertEqual(job.issue_date_policy, 'invoice')

    def test_current_eta_date_requires_accounting_approval_and_recorded_evidence(self):
        job = self.invoice().eta_job_ids
        user = new_test_user(self.env, login='eta_date_unprivileged', groups='base.group_user',
            company_id=self.company.id, company_ids=[Command.set(self.company.ids)])
        with self.assertRaises(AccessError):
            job.with_user(user).action_approve_current_issue_date()
        job.issue_date_policy = 'current'
        with self.assertRaises(UserError):
            job._prepare()
        self.assertFalse(job.payload or job.attempted_at)

    def test_approved_current_eta_date_is_frozen_for_json_and_canonical(self):
        issued = fields.Date.today() - timedelta(days=45)
        move = self.invoice(invoice_date=issued)
        job = move.eta_job_ids
        job.action_approve_current_issue_date()
        job.current_issue_datetime = fields.Datetime.now()
        expected = job.current_issue_datetime.strftime('%Y-%m-%dT%H:%M:%SZ')
        with patch.object(type(move), '_get_eta_invoice_lines', return_value=([], 0, 100, 100)), \
                patch.object(type(move), '_get_eta_personal_details', side_effect=lambda *a: {'address': {}}), \
                patch.object(type(move), '_get_eta_tax_totals', return_value=[]), \
                patch.object(type(move), 'inv_type', '', create=True), \
                patch.object(type(move), 'reference_number', '', create=True), \
                patch('requests.sessions.Session.request', side_effect=AssertionError('Generation performed HTTP')):
            document, _environment = move.action_generate_eta_json()
        self.assertEqual(document['dateTimeIssued'], expected)
        self.assertIn('"DATETIMEISSUED""%s"' % expected, move.e_invoice_canonical)
        self.assertEqual(move.invoice_date, issued)

    def test_approved_current_eta_date_submits_old_invoice_without_financial_changes(self):
        move = self.invoice(invoice_date=fields.Date.today() - timedelta(days=45))
        before = (move.invoice_date, move.amount_total, move.amount_tax, move.amount_untaxed, move.state)
        job = move.eta_job_ids
        job.action_approve_current_issue_date()
        client = MagicMock()
        client.request.return_value = {'acceptedDocuments': [{'uuid': 'approved-current-date', 'internalId': move.name}]}
        def signed():
            return {'internalID': move.name, 'dateTimeIssued': move._eta_issue_timestamp(),
                    'signatures': [{'type': 'I', 'value': 'test-signature'}]}
        with patch.object(type(move), 'action_generate_eta_json'), \
                patch.object(type(move), 'action_sign_invoice', side_effect=signed), patch.object(self.env.cr, 'commit'):
            job._submit(client)
        self.assertEqual(job.state, 'accepted')
        self.assertEqual(job.attempts, 1)
        self.assertEqual(json.loads(job.payload)['documents'][0]['dateTimeIssued'], move._eta_issue_timestamp())
        self.assertEqual(before, (move.invoice_date, move.amount_total, move.amount_tax, move.amount_untaxed, move.state))
        self.assertEqual(client.request.call_args.args[0], 'POST')

    def test_approved_current_eta_date_reprepares_after_calendar_rollover(self):
        move = self.invoice(invoice_date=fields.Date.today() - timedelta(days=45))
        job = move.eta_job_ids
        job.action_approve_current_issue_date()
        old_time = fields.Datetime.now() - timedelta(days=2)
        job.write({'current_issue_datetime': old_time, 'attempted_at': old_time,
            'last_error': 'ETA returned HTTP 429.', 'payload': json.dumps({'documents': [{
                'internalID': move.name, 'dateTimeIssued': old_time.strftime('%Y-%m-%dT%H:%M:%SZ')}]})})
        client = MagicMock()
        client.request.return_value = {'acceptedDocuments': [{'uuid': 'rollover-approved', 'internalId': move.name}]}
        def signed():
            return {'internalID': move.name, 'dateTimeIssued': move._eta_issue_timestamp(),
                    'signatures': [{'type': 'I', 'value': 'test-signature'}]}
        with patch.object(type(move), 'action_generate_eta_json') as generate, \
                patch.object(type(move), 'action_sign_invoice', side_effect=signed), patch.object(self.env.cr, 'commit'):
            job._submit(client)
        generate.assert_called_once()
        self.assertEqual(job.state, 'accepted')
        self.assertGreater(job.current_issue_datetime, old_time + timedelta(days=1))
        self.assertEqual(move.invoice_date, fields.Date.today() - timedelta(days=45))

    def test_current_eta_date_log_keeps_other_errors_visible(self):
        job = self.invoice().eta_job_ids
        job.action_approve_current_issue_date()
        job.write({'state': 'blocked', 'last_error': 'CF313 / dateTimeIssued; CF305 / receiver ID required'})
        self.assertIn('issue-date treatment is approved', job.required_action)
        self.assertIn('customer ID', job.required_action)
        self.assertNotIn('Finance must arrange', job.required_action)

    def test_current_eta_date_refuses_an_ambiguous_local_invoice_number(self):
        job = self.invoice().eta_job_ids
        with patch.object(type(self.env['account.move']), 'search_count', return_value=2):
            with self.assertRaisesRegex(UserError, 'not unique'):
                job.action_approve_current_issue_date()
        self.assertEqual(job.issue_date_policy, 'invoice')
        self.assertFalse(job.issue_date_approved_at)

    def test_verified_signature_only_rejection_can_be_resigned_without_fiscal_changes(self):
        move = self.invoice()
        move.write({'e_invoice_uuid': 'signature-old', 'e_invoice_status': 'Invalid', 'e_invoice_sent': True})
        original = {'internalID': move.name, 'totalAmount': 100, 'dateTimeIssued': '2026-01-01T00:00:00Z'}
        result = {'uuid': 'signature-old', 'internalId': move.name, 'status': 'Invalid', 'document': original,
                  'validationResults': {'errorCode': 'Err03', 'innerError': [{'errorCode': '4043'}]}}
        client = MagicMock(); client.request.return_value = result
        with patch('odoo.addons.tradeline_eta_reliability.models.account_move.EtaClient', return_value=client), \
                patch.object(type(move), '_eta_correction_document', return_value=original):
            move.action_send_electronic_invoice()
        self.assertFalse(move.e_invoice_uuid)
        self.assertEqual(move.eta_job_ids.state, 'queued')
        self.assertEqual(move.amount_total, 100)
        self.assertTrue(self.env['ir.attachment'].search_count([
            ('res_model', '=', 'account.move'), ('res_id', '=', move.id), ('name', '=', 'ETA_rejected_signature-old.json')]))
        self.assertEqual([c.args[0] for c in client.request.call_args_list], ['GET'])

    def test_signature_rejection_with_another_error_stays_held_if_source_unchanged(self):
        move = self.invoice()
        move.write({'e_invoice_uuid': 'signature-old', 'e_invoice_status': 'Invalid', 'e_invoice_sent': True})
        original = {'internalID': move.name, 'totalAmount': 100}
        client = MagicMock()
        client.request.return_value = {'uuid': 'signature-old', 'internalId': move.name, 'status': 'Invalid',
            'document': original, 'validationResults': {'steps': [
                {'errorCode': 'Err03', 'innerError': [{'errorCode': '4043'}]}, {'errorCode': 'NI310'}]}}
        with patch('odoo.addons.tradeline_eta_reliability.models.account_move.EtaClient', return_value=client), \
                patch.object(type(move), '_eta_correction_document', return_value=original):
            with self.assertRaises(UserError):
                move.action_send_electronic_invoice()
        self.assertEqual(move.e_invoice_uuid, 'signature-old')
        self.assertEqual([c.args[0] for c in client.request.call_args_list], ['GET'])

    def test_invoice_number_4043_is_not_a_signature_error_code(self):
        message = 'ETA rejected the submission (1). ' + json.dumps({
            'code': '1', 'target': 'BEVER/2026/4043', 'details': [{
                'code': 'CF313', 'target': 'dateTimeIssued', 'message': 'Issue date outside the submission window'}]})
        reason, codes = error_details(message)
        self.assertIn('CF313', codes)
        self.assertNotIn('4043', codes)
        self.assertIn('Issue date', reason)
        _, signature_codes = error_details("{'errorCode': '4043', 'error': 'Digest mismatch'}")
        self.assertIn('4043', signature_codes)

    def test_untaxed_discounted_eta_lines_match_posted_accounting_amounts(self):
        egp = self.env.ref('base.EGP'); egp.active = True
        metadata = {'e_invoicing_code': 'EG-test-discount', 'code_type': 'egs', 'ar_description': 'Test description'}
        product = self.env['product.product'].create({'name': 'ETA discount test product',
            **{key: value for key, value in metadata.items() if key in self.env['product.product']._fields}})
        eta_uom = self.env['eta.uom'].search([('code', '=', 'D41')], limit=1)
        if not eta_uom:
            eta_uom = self.env['eta.uom'].create({'name': 'Each', 'code': 'D41'})
        product.uom_id.eta_uom_id = eta_uom
        move = self.env['account.move'].create({'move_type': 'out_invoice', 'journal_id': self.journal.id,
            'partner_id': self.partner.id, 'currency_id': egp.id, 'invoice_date': fields.Date.today(),
            'invoice_line_ids': [Command.create({'product_id': product.id, 'account_id': self.income.id,
                'quantity': 2, 'price_unit': 1250, 'discount': 10, 'tax_ids': [Command.clear()]}),
                Command.create({'product_id': product.id, 'account_id': self.income.id,
                'quantity': 1, 'price_unit': 100, 'tax_ids': [Command.clear()]})]})
        move.action_post()
        before = (move.invoice_date, move.amount_total, move.amount_untaxed,
                  move.amount_tax, tuple(move.line_ids.mapped('balance')))
        with ExitStack() as stack:
            stack.enter_context(patch('requests.sessions.Session.request', side_effect=AssertionError('Serialization performed HTTP')))
            # The minimal QA stack lacks optional product master extensions;
            # supply their metadata while exercising the real line serializer.
            for key, value in metadata.items():
                if key not in product._fields:
                    stack.enter_context(patch.object(type(product), key, value, create=True))
            lines, discount, sales, total = move._get_eta_invoice_lines()
        self.assertEqual(lines[0]['salesTotal'], 2500)
        self.assertEqual(lines[0]['netTotal'], 2250)
        self.assertEqual(lines[0]['total'], 2250)
        self.assertEqual(discount, 250)
        self.assertEqual(sales, 2600)
        self.assertEqual(total, 2350)
        self.assertEqual(total, move.amount_total)
        self.assertEqual(before, (move.invoice_date, move.amount_total, move.amount_untaxed,
                  move.amount_tax, tuple(move.line_ids.mapped('balance'))))

    def _free_item_serializer_fixture(self, currency):
        currency.active = True
        metadata = {'e_invoicing_code': 'EG-test-free', 'code_type': 'egs', 'ar_description': 'Test free item'}
        product = self.env['product.product'].create({'name': 'ETA free item test',
            **{key: value for key, value in metadata.items() if key in self.env['product.product']._fields}})
        eta_uom = self.env['eta.uom'].search([('code', '=', 'D41')], limit=1)
        eta_uom = eta_uom or self.env['eta.uom'].create({'name': 'Each', 'code': 'D41'})
        product.uom_id.eta_uom_id = eta_uom
        vat = self.env['account.tax'].create({'name': 'ETA free item VAT', 'amount': 14,
            'amount_type': 'percent', 'type_tax_use': 'sale', 'price_include_override': 'tax_included'})
        specs = [(1, 114, 0, vat), (2, 0, 0, vat), (3, 0, 0, self.env['account.tax']), (1, 114, 100, vat)]
        move = self.env['account.move'].create({'move_type': 'out_invoice', 'journal_id': self.journal.id,
            'partner_id': self.partner.id, 'currency_id': currency.id, 'invoice_date': fields.Date.today(),
            'invoice_line_ids': [Command.create({'product_id': product.id, 'account_id': self.income.id,
                'quantity': qty, 'price_unit': price, 'discount': disc, 'tax_ids': [Command.set(tax.ids)]})
                for qty, price, disc, tax in specs]})
        move.action_post()
        before = (move.invoice_date, move.amount_total, move.amount_untaxed, move.amount_tax,
                  tuple(move.line_ids.mapped('balance')))
        with ExitStack() as stack:
            stack.enter_context(patch('requests.sessions.Session.request', side_effect=AssertionError('Serialization performed HTTP')))
            for key, value in metadata.items():
                if key not in product._fields:
                    stack.enter_context(patch.object(type(product), key, value, create=True))
            values = move._get_eta_invoice_lines()
        self.assertEqual(before, (move.invoice_date, move.amount_total, move.amount_untaxed,
                                  move.amount_tax, tuple(move.line_ids.mapped('balance'))))
        return move, values

    def test_free_egp_items_have_zero_unit_values_and_balanced_line_totals(self):
        move, (lines, discount, sales, total) = self._free_item_serializer_fixture(self.env.ref('base.EGP'))
        for index in (1, 2):
            self.assertEqual(lines[index]['unitValue']['amountEGP'], 0)
            self.assertEqual(lines[index]['salesTotal'], lines[index]['quantity'] * lines[index]['unitValue']['amountEGP'])
            self.assertEqual(lines[index]['netTotal'], 0)
            self.assertEqual(lines[index]['total'], 0)
        # A paid-price item with a full discount is distinct from a zero-price item.
        self.assertEqual(lines[3]['unitValue']['amountEGP'], 100)
        self.assertEqual(lines[3]['salesTotal'], 100)
        self.assertEqual(lines[3]['discount']['amount'], 100)
        self.assertAlmostEqual(discount, 100, places=4)
        self.assertAlmostEqual(sales, 200, places=4)
        self.assertEqual(total, move.amount_total)

    def test_free_foreign_currency_items_do_not_invent_amount_sold(self):
        _move, (lines, _discount, _sales, _total) = self._free_item_serializer_fixture(self.env.ref('base.USD'))
        for index in (1, 2):
            self.assertEqual(lines[index]['unitValue']['amountSold'], 0)
            self.assertEqual(lines[index]['unitValue']['amountEGP'], 0)
            self.assertEqual(lines[index]['salesTotal'], 0)
            self.assertEqual(lines[index]['total'], 0)
        self.assertEqual(lines[3]['unitValue']['amountSold'], 100)

    def test_eta_canonical_preserves_json_escapes_and_numeric_representation(self):
        move = self.invoice()
        doc = {'description': 'Tablet 10.2"', 'receiver': {'name': 'A\\B\nC'},
               'lines': [{'quantity': 1.0, 'netTotal': 0.0}], 'internalID': 'ABC'}
        expected = (r'"DESCRIPTION""Tablet 10.2\"""RECEIVER""NAME""A\\B\nC"'
                    '"LINES""LINES""QUANTITY""1.0""NETTOTAL""0.0""INTERNALID""ABC"')
        self.assertEqual(move.generate_canonical_manual(doc), expected)

    def test_quote_description_preparation_keeps_fiscal_text_and_signs_escaped_bytes(self):
        move = self.invoice(); job = move.eta_job_ids
        before = (move.invoice_date, move.amount_total, move.amount_tax, move.state)
        description = 'Tablet 10.2"'
        with patch.object(type(move), '_get_eta_invoice_lines', return_value=([{'description': description}], 0, 100, 100)), \
                patch.object(type(move), '_get_eta_personal_details', side_effect=lambda *a: {'address': {}}), \
                patch.object(type(move), '_get_eta_tax_totals', return_value=[]), \
                patch.object(type(move), 'inv_type', '', create=True), \
                patch.object(type(move), 'reference_number', '', create=True), \
                patch.object(type(move), 'get_signature_value', return_value='test-signature'), \
                patch('requests.sessions.Session.request', side_effect=AssertionError('Preparation performed HTTP')):
            job._prepare()
        self.assertEqual(json.loads(job.payload)['documents'][0]['invoiceLines'][0]['description'], description)
        self.assertIn(r'"DESCRIPTION""Tablet 10.2\""', move.e_invoice_canonical)
        self.assertEqual(before, (move.invoice_date, move.amount_total, move.amount_tax, move.state))

    def test_cms_message_digest_must_match_the_document_before_transport(self):
        move = self.invoice(); move.e_invoice_canonical = '"DOCUMENT""test"'
        def signature(digest):
            value = cms.ContentInfo({'content_type': 'signed_data', 'content': {
                'version': 'v3', 'digest_algorithms': [{'algorithm': 'sha256'}],
                'encap_content_info': {'content_type': 'data'}, 'signer_infos': [{
                    'version': 'v3', 'sid': {'subject_key_identifier': b'test-key'},
                    'digest_algorithm': {'algorithm': 'sha256'},
                    'signed_attrs': [{'type': 'message_digest', 'values': [digest]}],
                    'signature_algorithm': {'algorithm': 'rsassa_pkcs1v15'}, 'signature': b'test-only'}]}})
            return base64.b64encode(value.dump()).decode()
        matching = signature(hashlib.sha256(move.e_invoice_canonical.encode()).digest())
        self.assertTrue(move._eta_validate_signature_digest(matching))
        for rejected in (signature(b'0' * 32), 'not base64'):
            with self.assertRaisesRegex(UserError, 'does not match'):
                move._eta_validate_signature_digest(rejected)

    def test_invalid_signing_response_is_stopped_before_eta_post_or_attempt_marker(self):
        move = self.invoice(); job = move.eta_job_ids; client = MagicMock()
        def generate():
            move.write({'e_invoice_json': str({'internalID': move.name}),
                        'e_invoice_canonical': '"INTERNALID""%s"' % move.name})
        with patch.object(type(move), 'action_generate_eta_json', side_effect=generate), \
                patch('odoo.addons.tradeline_eta_reliability.models.account_move.requests.get',
                      return_value=SimpleNamespace(status_code=200, text='not-base64')):
            with self.assertRaisesRegex(UserError, 'Nothing was sent'):
                job._submit(client)
        client.request.assert_not_called()
        self.assertFalse(job.attempted_at or job.payload)
        self.assertEqual(job.attempts, 0)

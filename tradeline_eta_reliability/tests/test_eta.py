import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from ..models.client import EtaClient, EtaError


@tagged('post_install', '-at_install')
class TestEtaReliability(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
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

    def test_historical_backlog_does_not_starve_new_submissions_or_statuses(self):
        for state in ('queued', 'accepted', 'unknown'):
            self.invoice().eta_job_ids.write({'state': state, 'next_attempt': False})
        due = self.env['tradeline.eta.job']._due_jobs('all')
        self.assertEqual([job.state for job in due[:3]], ['queued', 'accepted', 'unknown'])

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
                   return_value=SimpleNamespace(status_code=200, text='signed')) as http:
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

    def test_old_invoice_is_blocked_without_rewriting_date(self):
        old = fields.Date.today() - timedelta(days=30)
        move = self.invoice(invoice_date=old)
        with self.assertRaises(UserError):
            move.eta_job_ids._prepare()
        self.assertEqual(move.invoice_date, old)

    def test_generated_document_and_canonical_use_invoice_date_instead_of_submission_day(self):
        issued = fields.Date.today() - timedelta(days=1)
        move = self.invoice(invoice_date=issued)
        with patch.object(type(move), '_get_eta_invoice_lines', return_value=([], 0, 100, 100)), \
                patch.object(type(move), '_get_eta_personal_details', side_effect=lambda *a: {'address': {}}), \
                patch.object(type(move), '_get_eta_tax_totals', return_value=[]), \
                patch.object(type(move), 'inv_type', '', create=True), \
                patch.object(type(move), 'reference_number', '', create=True), \
                patch('requests.sessions.Session.request', side_effect=AssertionError('Generation performed HTTP')):
            document, _environment = move.action_generate_eta_json()
        expected = str(issued) + 'T00:00:00Z'
        self.assertEqual(document['dateTimeIssued'], expected)
        self.assertIn('"DATETIMEISSUED""%s"' % expected, move.e_invoice_canonical)
        self.assertEqual(move.invoice_date, issued)

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

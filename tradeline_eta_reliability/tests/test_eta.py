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
                   'payload': json.dumps({'documents': [{'internalID': move.name}]})})
        client = MagicMock()
        client.request.return_value = {'result': [], 'metadata': {'continuationToken': 'EndofResultSet'}}
        job._refresh(client)
        self.assertEqual(job.state, 'unknown')
        self.assertTrue(all(c.args[0] == 'GET' for c in client.request.call_args_list))
        self.assertFalse(move.e_invoice_uuid)

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
            'submissionUUID': 'verified-submission', 'document': json.dumps({'internalID': move.name})})
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

    def test_ambiguous_post_marker_survives_transport_failure(self):
        move = self.invoice()
        job = move.eta_job_ids
        job.payload = json.dumps({'documents': [{'internalID': move.name}]})
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
        job.payload = json.dumps({'documents': [{'internalID': move.name}]})
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
        job.payload = json.dumps({'documents': [{'internalID': move.name}]})
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

import hashlib
import json
import sqlite3
from contextlib import ExitStack, closing
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from ..models.client import EtaClient, EtaError
from ..models.historical_recovery import fiscal, fiscal_equal, leaf_codes, match_document


@tagged('post_install', '-at_install')
class TestHistoricalRecovery(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.env.ref('base.EGP').active = True
        cls.company.write({'client_id': 'test-client', 'client_secret': 'test-secret', 'config_type': 'uat'})
        cls.receivable = cls.env['account.account'].create({'name': 'History test receivable', 'code': 'HTRCV',
            'account_type': 'asset_receivable', 'reconcile': True, 'company_ids': [Command.set(cls.company.ids)]})
        cls.income = cls.env['account.account'].create({'name': 'History test income', 'code': 'HTINC',
            'account_type': 'income', 'company_ids': [Command.set(cls.company.ids)]})
        cls.partner = cls.env['res.partner'].create({'name': 'History test buyer', 'property_account_receivable_id': cls.receivable.id})
        cls.journal = cls.env['account.journal'].create({'name': 'History test', 'code': 'HIST', 'type': 'sale',
            'company_id': cls.company.id, 'edi_format_ids': [Command.clear()]})

    def invoice(self, age=0):
        move = self.env['account.move'].create({'move_type': 'out_invoice', 'journal_id': self.journal.id,
            'currency_id': self.env.ref('base.EGP').id,
            'partner_id': self.partner.id, 'invoice_date': fields.Date.today() - timedelta(days=age),
            'invoice_line_ids': [Command.create({'name': 'History test item', 'account_id': self.income.id,
                'quantity': 1, 'price_unit': 100, 'tax_ids': [Command.clear()]})]})
        move.action_post(); return move

    def document(self, move):
        return {'internalID': move.name, 'dateTimeIssued': str(move.invoice_date) + 'T00:00:00Z',
            'documentType': 'I', 'issuer': {'id': '123456789', 'address': {'country': 'EG', 'street': 'Street', 'regionCity': 'Cairo'}},
            'receiver': {'type': 'P', 'id': '', 'name': 'History test buyer', 'address': {'country': 'EG', 'street': 'Street'}},
            'invoiceLines': [{'itemCode': 'TEST', 'internalCode': 'TEST', 'itemType': 'EGS', 'description': 'Test item',
                             'quantity': 1, 'unitValue': {'currencySold': 'EGP', 'amountEGP': 100}, 'total': 100}],
            'totalAmount': 100, 'netAmount': 100, 'taxTotals': []}

    def review(self, move, receipts=(), proof=True, match=True):
        job = move.eta_job_ids
        job.write({'state': 'review', 'historical_review': True})
        client = MagicMock(); source = self.document(move)
        history = {'from': fields.Datetime.now() - timedelta(days=400), 'to': fields.Datetime.now() - timedelta(minutes=1), 'receipts': {}} if proof else None
        with ExitStack() as stack:
            stack.enter_context(patch.object(type(move), 'currency_id', self.env.ref('base.EGP')))
            stack.enter_context(patch.object(type(job), '_historical_source', return_value=source))
            stack.enter_context(patch.object(type(job), '_history_snapshot', return_value=history))
            candidates = stack.enter_context(patch.object(type(job), '_history_candidates', return_value=list(receipts)))
            stack.enter_context(patch.object(type(job), '_historical_matches', return_value=match))
            job._review_historical(client)
        self.assertFalse(any(c.args[0] == 'POST' for c in client.request.call_args_list))
        return job, client, candidates

    def test_full_enrollment_is_database_scoped_and_never_sends_http(self):
        move = self.invoice(); move.eta_job_ids.unlink(); before = (move.invoice_date, move.amount_total)
        jobs = self.env['tradeline.eta.job']
        self.assertEqual(jobs._enroll_historical(), 0)
        self.env['ir.config_parameter'].sudo().set_param('tradeline_eta_reliability.historical_recovery_database', self.env.cr.dbname)
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Enrollment sent HTTP')):
            jobs._enroll_historical(move_ids=move.ids); jobs._enroll_historical(move_ids=move.ids)
        self.assertEqual(len(move.eta_job_ids), 1)
        self.assertEqual(move.eta_job_ids.state, 'review')
        self.assertTrue(move.eta_job_ids.historical_fingerprint)
        self.assertEqual(before, (move.invoice_date, move.amount_total))

    def test_incomplete_history_is_waiting_and_cannot_create_a_submission(self):
        move = self.invoice(); job, client, candidates = self.review(move, proof=False)
        self.assertEqual(job.state, 'review'); self.assertTrue(job.next_attempt)
        candidates.assert_not_called(); client.request.assert_not_called()

    def test_unscoped_enrollment_accepts_null_filter_without_http_or_accounting_edits(self):
        move = self.invoice(); before = (move.invoice_date, move.amount_total, move.amount_tax, move.state)
        self.env['ir.config_parameter'].sudo().set_param('tradeline_eta_reliability.historical_recovery_database', self.env.cr.dbname)
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Enrollment sent HTTP')):
            self.env['tradeline.eta.job']._enroll_historical(limit=0)
        self.assertEqual(before, (move.invoice_date, move.amount_total, move.amount_tax, move.state))

    def test_complete_absence_queues_old_invoice_with_approved_date_only(self):
        move = self.invoice(age=10); before = (move.invoice_date, move.amount_total, move.amount_tax, move.state)
        job, client, _ = self.review(move)
        self.assertEqual(job.state, 'queued'); self.assertTrue(job.historical_submission_verified)
        self.assertEqual(job.issue_date_policy, 'current')
        self.assertFalse(job.attempted_at or move.e_invoice_uuid)
        self.assertEqual(before, (move.invoice_date, move.amount_total, move.amount_tax, move.state))
        client.request.assert_not_called()

    def test_selected_canary_scans_full_history_without_trusting_partial_index(self):
        move = self.invoice(age=10)
        self.env['ir.config_parameter'].sudo().set_param('tradeline_eta_reliability.historical_canary_move_ids', json.dumps([move.id]))
        job, _, candidates = self.review(move, proof=False)
        self.assertEqual(job.state, 'queued')
        proof = candidates.call_args.args[1]
        self.assertEqual(proof['from'], proof['to'])
        self.assertEqual(str(proof['from'].date()), '2025-12-01')

    def test_existing_approval_is_restored_without_another_post(self):
        move = self.invoice(); before = (move.invoice_date, move.amount_total, move.state)
        move.write({'e_invoice_uuid': 'old-invalid-copy', 'e_invoice_status': 'Invalid', 'e_invoice_sent': True})
        raw = {'uuid': 'original-approved', 'internalId': move.name, 'status': 'Valid', 'document': self.document(move)}
        job, client, _ = self.review(move, [raw])
        self.assertEqual((job.state, move.e_invoice_uuid), ('valid', 'original-approved'))
        self.assertEqual(before, (move.invoice_date, move.amount_total, move.state))
        self.assertEqual(move.l10n_eg_uuid, 'original-approved')
        client.request.assert_not_called()

    def test_generated_amount_mismatch_is_stopped_before_authority_checks(self):
        move = self.invoice(); job = move.eta_job_ids; source = self.document(move)
        source['totalAmount'] = 101
        job.write({'state': 'review', 'historical_review': True})
        client = MagicMock()
        with patch.object(type(move), 'currency_id', self.env.ref('base.EGP')), \
                patch.object(type(job), '_historical_source', return_value=source), \
                patch.object(type(job), '_history_snapshot') as proof:
            job._review_historical(client)
        self.assertEqual(job.state, 'blocked'); self.assertIn('serialization', job.last_error)
        self.assertEqual(move.amount_total, 100); proof.assert_not_called(); client.request.assert_not_called()

    def test_receipt_matching_preserves_withholding_tax_signs(self):
        move = self.invoice(); move.button_draft()
        withholding = self.env['account.tax'].create({'name': 'History withholding', 'amount': -10,
            'amount_type': 'percent', 'type_tax_use': 'sale', 'company_id': self.company.id})
        move.invoice_line_ids.tax_ids = withholding
        move.action_post(); self.assertEqual(move.amount_total, 90)
        source = self.document(move); source.update(totalAmount=90, netAmount=100,
            taxTotals=[{'taxType': 'T4', 'amount': 10}])
        raw = {'document': source}
        with patch.object(type(self.company), 'vat', '123456789'), \
                patch.object(type(self.env['product.product']), 'search_count', return_value=0):
            self.assertTrue(move.eta_job_ids._historical_matches(raw, source))

    def test_approved_source_mismatch_keeps_original_identifiers_and_holds(self):
        move = self.invoice(); move.e_invoice_uuid = 'known-copy'
        raw = {'uuid': 'original-approved', 'internalId': move.name, 'status': 'Valid', 'document': self.document(move)}
        raw['document']['totalAmount'] = 200
        job, _, _ = self.review(move, [raw], match=False)
        self.assertEqual(job.state, 'blocked'); self.assertEqual(move.e_invoice_uuid, 'known-copy')
        self.assertIn('totalAmount', job.last_error)

    def test_multiple_approvals_and_cancelled_originals_are_never_reissued(self):
        for statuses in (['Valid', 'Valid'], ['Cancelled']):
            move = self.invoice()
            receipts = [{'uuid': 'candidate-%s' % i, 'internalId': move.name, 'status': status, 'document': self.document(move)} for i, status in enumerate(statuses)]
            job, _, _ = self.review(move, receipts)
            self.assertEqual(job.state, 'blocked'); self.assertFalse(job.historical_submission_verified)

    def test_unchanged_receiver_rejection_remains_an_error(self):
        move = self.invoice()
        raw = {'uuid': 'invalid-receipt', 'internalId': move.name, 'status': 'Invalid', 'document': self.document(move),
               'validationResults': {'errorCode': 'Err04', 'innerError': [{'errorCode': 'NI310', 'error': 'Invalid customer ID'}]}}
        job, _, _ = self.review(move, [raw])
        self.assertEqual(job.state, 'invalid'); self.assertIn('NI310', job.last_error)
        self.assertFalse(job.historical_submission_verified)

    def test_optional_id_only_rejection_is_recovered_after_complete_history(self):
        move = self.invoice(); source = self.document(move); source['receiver']['id'] = '1'
        before = (move.invoice_date, move.amount_total, move.amount_tax)
        raw = {'uuid': 'bad-id', 'internalId': move.name, 'status': 'Invalid', 'document': source,
               'validationResults': {'errorCode': 'Err04', 'innerError': [{'errorCode': 'NI310'}, {'errorCode': 'SF307'}]}}
        with patch.object(self, 'document', return_value=source):job, client, _ = self.review(move, [raw])
        self.assertEqual(job.state, 'queued'); self.assertTrue(job.omit_optional_receiver_id)
        self.assertTrue(job.historical_submission_verified)
        self.assertEqual(before, (move.invoice_date, move.amount_total, move.amount_tax))
        client.request.assert_not_called()
        self.assertTrue(self.env['ir.attachment'].search_count([('res_model', '=', 'account.move'), ('res_id', '=', move.id)]))

    def test_optional_id_recovery_rejects_mixed_product_errors_and_required_ids(self):
        move = self.invoice(); source = self.document(move); source['receiver']['id'] = '1'; job = move.eta_job_ids
        ni = {'innerError': [{'errorCode': 'NI303'}]}
        self.assertTrue(job._optional_person_id_rejection(source, source, ni))
        mixed = {'innerError': [{'errorCode': 'NI303'}, {'errorCode': 'CV302'}]}
        self.assertFalse(job._optional_person_id_rejection(source, source, mixed))
        for change in ({'type': 'B'}, {'type': 'F'}, {'address': {'country': 'US'}}):
            other = json.loads(json.dumps(source)); other['receiver'].update(change)
            self.assertFalse(job._optional_person_id_rejection(other, source, ni))
        high = json.loads(json.dumps(source)); high.update(totalAmount=25000, netAmount=25000)
        self.assertFalse(job._optional_person_id_rejection(high, source, ni))
        raw = {'uuid': 'mixed', 'internalId': move.name, 'status': 'Invalid', 'document': source, 'validationResults': mixed}
        with patch.object(self, 'document', return_value=source):job, _, _ = self.review(move, [raw])
        self.assertEqual(job.state, 'invalid'); self.assertFalse(job.omit_optional_receiver_id)

    def test_optional_id_recovery_does_not_bypass_unknown_or_incomplete_proof(self):
        move = self.invoice(); job = move.eta_job_ids; source = self.document(move); source['receiver']['id'] = '1'
        job.write({'attempted_at': fields.Datetime.now(), 'payload': json.dumps({'documents': [source]}),
            'last_error': 'ETA rejected the submission (1). ' + json.dumps({'code': '1', 'details': [{'code': 'NI310'}]})})
        with patch.object(self, 'document', return_value=source):job, client, _ = self.review(move)
        self.assertEqual(job.state, 'review'); self.assertFalse(job.omit_optional_receiver_id)
        self.assertIn('uncertain', job.last_error); client.request.assert_not_called()
        job.write({'safe_to_retry': True, 'last_error': 'ETA rejected the submission (1). ' + json.dumps({'code': '1', 'details': [{'code': 'NI310'}]})})
        with patch.object(self, 'document', return_value=source):job, client, _ = self.review(move, proof=False)
        self.assertEqual(job.state, 'review'); self.assertFalse(job.omit_optional_receiver_id)
        client.request.assert_not_called()
        with patch.object(self, 'document', return_value=source):job, client, _ = self.review(move)
        self.assertEqual(job.state, 'queued'); self.assertTrue(job.omit_optional_receiver_id)
        self.assertFalse(job.attempted_at); client.request.assert_not_called()

    def test_approved_anonymous_person_receipt_keeps_strict_nonempty_identity_checks(self):
        move = self.invoice(); job = move.eta_job_ids; original = self.document(move)
        current = json.loads(json.dumps(original)); current['receiver']['id'] = '1'
        with patch.object(type(self.company), 'vat', '123456789'), patch.object(type(self.env['product.product']), 'search_count', return_value=0):
            self.assertTrue(job._historical_matches({'status': 'Valid', 'document': original}, current))
            self.assertFalse(job._historical_matches({'status': 'Submitted', 'document': original}, current))
            conflicting = json.loads(json.dumps(original)); conflicting['receiver']['id'] = '12345678901234'
            self.assertFalse(job._historical_matches({'status': 'Valid', 'document': conflicting}, current))
            current['receiver']['name'] = 'Different person'
            self.assertFalse(job._historical_matches({'status': 'Valid', 'document': original}, current))
        self.assertEqual(original['receiver']['id'], '')

    def test_optional_id_policy_does_not_apply_to_other_invalid_validation_steps(self):
        move = self.invoice(); job = move.eta_job_ids; doc = self.document(move); doc['receiver']['id'] = '1'
        profile = {'status': 'Invalid', 'validationSteps': [{'name': 'Step-05. TaxpayerProfile Validator', 'status': 'Invalid'}]}
        self.assertTrue(job._optional_person_id_rejection(doc, doc, profile))
        profile['validationSteps'].append({'name': 'Step-03. ITIDA Signature Validator', 'status': 'Invalid'})
        self.assertFalse(job._optional_person_id_rejection(doc, doc, profile))

    def test_date_only_rejection_is_recoverable_after_complete_history(self):
        move = self.invoice(age=10); move.write({'e_invoice_uuid': 'date-rejection', 'e_invoice_status': 'Invalid', 'e_invoice_sent': True})
        raw = {'uuid': 'date-rejection', 'internalId': move.name, 'status': 'Invalid', 'document': self.document(move),
               'validationResults': {'errorCode': 'Err01', 'innerError': [{'errorCode': 'CF313'}]}}
        job, _, _ = self.review(move, [raw])
        self.assertEqual(job.state, 'queued'); self.assertEqual(job.issue_date_policy, 'current')
        self.assertFalse(move.e_invoice_uuid); self.assertTrue(job.historical_submission_verified)

    def test_recent_uncertain_post_stays_in_reconciliation_window(self):
        move = self.invoice(); job = move.eta_job_ids
        job.attempted_at = fields.Datetime.now(); before = job.attempted_at
        job, _, _ = self.review(move)
        self.assertEqual(job.state, 'review'); self.assertEqual(job.attempted_at, before)
        self.assertFalse(job.historical_submission_verified)

    def test_manual_retry_cannot_bypass_incomplete_historical_proof(self):
        move = self.invoice(); job = move.eta_job_ids
        job.write({'state': 'blocked', 'historical_fingerprint': job._historical_financial_fingerprint(),
                   'historical_reviewed_at': fields.Datetime.now(), 'historical_submission_verified': False})
        job.action_recheck(); client = MagicMock()
        with patch.object(type(move), 'currency_id', self.env.ref('base.EGP')), \
                patch.object(type(job), '_historical_source', return_value=self.document(move)), \
                patch.object(type(job), '_history_snapshot', return_value=None):
            job._submit(client)
        self.assertEqual(job.state, 'review'); client.request.assert_not_called()

    def test_stale_queue_proof_is_refreshed_before_any_post(self):
        move = self.invoice(); job = move.eta_job_ids
        job.write({'historical_fingerprint': job._historical_financial_fingerprint(), 'historical_submission_verified': True,
                   'historical_reviewed_at': fields.Datetime.now() - timedelta(minutes=16)})
        client = MagicMock()
        with patch.object(type(move), 'currency_id', self.env.ref('base.EGP')), \
                patch.object(type(job), '_historical_source', return_value=self.document(move)), \
                patch.object(type(job), '_history_snapshot', return_value=None):
            job._submit(client)
        self.assertEqual(job.state, 'review'); client.request.assert_not_called()

    def test_history_pagination_requires_explicit_end_marker(self):
        move = self.invoice(); job = move.eta_job_ids; client = MagicMock()
        client.request.return_value = {'result': [], 'metadata': {}}
        proof = {'receipts': {}, 'to': fields.Datetime.now() - timedelta(minutes=1)}
        with self.assertRaises(EtaError):job._history_candidates(client, proof)
        self.assertTrue(all(call.args[0] == 'GET' for call in client.request.call_args_list))

    def test_duplicate_reference_is_checked_even_outside_cached_index(self):
        move = self.invoice(); job = move.eta_job_ids; client = MagicMock()
        original = 'ORIGINAL12345678901234567890'
        client.request.side_effect = [
            {'result': [], 'metadata': {'continuationToken': 'EndofResultSet'}},
            {'uuid': 'copy', 'internalId': move.name, 'status': 'Invalid', 'validationResults': {'errorCode': 'DS301', 'error': 'Duplicate UUID: ' + original}},
            {'uuid': original, 'internalId': move.name, 'status': 'Valid', 'document': self.document(move)}]
        result = job._history_candidates(client, {'receipts': {'copy': 'Invalid'}, 'to': fields.Datetime.now() - timedelta(minutes=1)})
        self.assertEqual([r['uuid'] for r in result], ['copy', original])
        self.assertTrue(all(call.args[0] == 'GET' for call in client.request.call_args_list))

    def test_historical_pages_and_raw_receipts_keep_transport_rate_limits(self):
        move = self.invoice(); job = move.eta_job_ids; client = EtaClient(self.company); client.token = 'test-token'
        clock = [100.0]; starts = []
        responses = [
            {'result': [], 'metadata': {'continuationToken': 'next-page'}},
            {'result': [], 'metadata': {'continuationToken': 'EndofResultSet'}},
            {'uuid': 'approved', 'internalId': move.name, 'status': 'Valid', 'document': self.document(move)}]
        def request(method, url, **kwargs):
            self.assertEqual(method, 'GET')
            starts.append(clock[0]); clock[0] += .2
            response = MagicMock(); response.ok = True; response.json.return_value = responses.pop(0)
            return response
        def sleep(seconds):
            clock[0] += seconds
        try:
            with patch.object(client, '_authorize'), patch.object(client.session, 'request', side_effect=request), \
                    patch('odoo.addons.tradeline_eta_reliability.models.client.time.monotonic', side_effect=lambda: clock[0]), \
                    patch('odoo.addons.tradeline_eta_reliability.models.client.time.sleep', side_effect=sleep):
                result = job._history_candidates(client, {'receipts': {'approved': 'Valid'},
                    'to': fields.Datetime.now() - timedelta(minutes=1)})
            self.assertEqual([r['uuid'] for r in result], ['approved'])
            self.assertEqual(len(starts), 3)
            self.assertAlmostEqual(starts[0], 102.1)
            self.assertGreaterEqual(starts[1] - starts[0], 2.1)
            self.assertAlmostEqual(starts[2] - starts[1], .6)
        finally:
            client.close()

    def test_history_index_cannot_cross_companies_or_databases(self):
        move = self.invoice(); job = move.eta_job_ids
        with TemporaryDirectory() as folder:
            path = str(Path(folder) / 'proof.sqlite3')
            with closing(sqlite3.connect(path)) as db, db:
                db.executescript('CREATE TABLE meta(key TEXT,value TEXT);CREATE TABLE scans(company INTEGER,history_from TEXT,history_to TEXT,complete INTEGER,updated_at TEXT);CREATE TABLE receipts(company INTEGER,uuid TEXT,invoice TEXT,status TEXT);')
                db.execute('INSERT INTO meta VALUES(?,?)', ['database', 'wrong-database'])
            with patch('odoo.addons.tradeline_eta_reliability.models.historical_recovery.HISTORY_INDEX', path):
                with self.assertRaises(UserError):job._history_snapshot()
                with closing(sqlite3.connect(path)) as db, db:
                    db.execute("UPDATE meta SET value=? WHERE key='database'", [self.env.cr.dbname])
                    db.execute('INSERT INTO scans VALUES(?,?,?,?,?)', [self.company.id, '2025-12-01', '2026-10-29', 0, str(fields.Datetime.now())])
                self.assertIsNone(job._history_snapshot())

    def test_corrected_source_reenters_automatic_historical_review(self):
        move = self.invoice(); job = move.eta_job_ids
        job.write({'state': 'blocked', 'historical_fingerprint': job._historical_financial_fingerprint(),
                   'historical_reviewed_at': fields.Datetime.now() - timedelta(minutes=5), 'historical_source_hash': 'prior-source'})
        with patch.object(type(job), '_historical_source', return_value=self.document(move)):
            job._requeue_changed_historical()
        self.assertEqual(job.state, 'review'); self.assertTrue(job.historical_review)
        self.assertFalse(job.historical_submission_verified)

    def test_status_only_write_does_not_loop_an_unchanged_rejection(self):
        move = self.invoice(); job = move.eta_job_ids; source = self.document(move)
        digest = hashlib.sha256(json.dumps(fiscal(source), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        job.write({'state': 'invalid', 'historical_fingerprint': job._historical_financial_fingerprint(),
                   'historical_reviewed_at': fields.Datetime.now() - timedelta(minutes=5), 'historical_source_hash': digest})
        with patch.object(type(job), '_historical_source', return_value=source):
            job._requeue_changed_historical()
        self.assertEqual(job.state, 'invalid'); self.assertFalse(job.historical_review)

    def test_completed_history_is_bound_to_taxpayer_and_environment(self):
        move = self.invoice(); job = move.eta_job_ids; rin = '123456789'
        scope = hashlib.sha256((self.company.config_type + '|' + rin).encode()).hexdigest()
        with TemporaryDirectory() as folder:
            path = str(Path(folder) / 'proof.sqlite3')
            with closing(sqlite3.connect(path)) as db, db:
                db.executescript('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);CREATE TABLE scans(company INTEGER,history_from TEXT,history_to TEXT,complete INTEGER,updated_at TEXT);CREATE TABLE receipts(company INTEGER,uuid TEXT,invoice TEXT,status TEXT);')
                db.execute('INSERT INTO meta VALUES(?,?)', ['database', self.env.cr.dbname])
                db.execute('INSERT INTO scans VALUES(?,?,?,?,?)', [self.company.id, '2025-12-01', '2026-10-29', 1, str(fields.Datetime.now())])
                db.execute('INSERT INTO receipts VALUES(?,?,?,?)', [self.company.id, 'own-uuid', move.name, 'Invalid'])
                db.execute('INSERT INTO receipts VALUES(?,?,?,?)', [self.company.id + 100, 'other-company', move.name, 'Valid'])
            with patch('odoo.addons.tradeline_eta_reliability.models.historical_recovery.HISTORY_INDEX', path), \
                    patch.object(type(self.company), 'vat', rin):
                with self.assertRaises(UserError):job._history_snapshot()
                with closing(sqlite3.connect(path)) as db, db:
                    db.execute('INSERT INTO meta VALUES(?,?)', ['scope:%s' % self.company.id, scope])
                self.assertEqual(job._history_snapshot()['receipts'], {'own-uuid': 'Invalid'})

    def test_wrong_authority_receipt_identity_cannot_be_reconciled(self):
        move = self.invoice(); job = move.eta_job_ids; client = MagicMock()
        client.request.side_effect = [{'result': [], 'metadata': {'continuationToken': 'EndofResultSet'}},
                                     {'uuid': 'wrong-uuid', 'internalId': move.name, 'status': 'Valid'}]
        with self.assertRaises(UserError):job._history_candidates(client, {'receipts': {'expected-uuid': 'Valid'}, 'to': fields.Datetime.now() - timedelta(minutes=1)})
        self.assertFalse(move.e_invoice_uuid)

    def test_failed_history_review_is_visible_in_operations_attention(self):
        move = self.invoice(); job = move.eta_job_ids
        job.write({'state': 'review', 'historical_review': True, 'failures': 1, 'last_error': 'Authority history scan stopped advancing.'})
        self.assertTrue(job.needs_attention)
        self.assertIn(job, self.env['tradeline.eta.job'].search([('needs_attention', '=', True)]))
        self.assertIn('IT must restore', job.required_action)

    def test_restored_original_does_not_keep_a_rejected_attempt_timestamp(self):
        move = self.invoice(age=10); job = move.eta_job_ids
        job.write({'issue_date_policy': 'current', 'current_issue_datetime': fields.Datetime.now(),
                   'issue_date_approved_by': self.env.uid, 'issue_date_approved_at': fields.Datetime.now(), 'issue_date_approval_note': 'Test approval'})
        raw = {'uuid': 'old-approved-original', 'internalId': move.name, 'status': 'Valid', 'document': self.document(move)}
        job, _, _ = self.review(move, [raw])
        self.assertEqual(job.state, 'valid'); self.assertFalse(job.current_issue_datetime)
        self.assertEqual(job.issue_date_policy, 'invoice')
        self.assertEqual(json.loads(job.payload)['documents'][0]['dateTimeIssued'], raw['document']['dateTimeIssued'])

    def test_fiscal_matching_allows_only_verified_metadata_and_subcent_money(self):
        move = self.invoice(); original = self.document(move); current = json.loads(json.dumps(original))
        current['totalAmount'] += .004
        self.assertTrue(match_document(original, current))
        current['invoiceLines'][0]['quantity'] += .004
        self.assertFalse(match_document(original, current))
        current = json.loads(json.dumps(original)); current['receiver']['id'] = '12345678901234'
        self.assertFalse(match_document(original, current))
        current = json.loads(json.dumps(original)); current['invoiceLines'][0]['itemCode'] = 'OTHER'
        self.assertFalse(match_document(original, current, safe_codes=['TEST', 'OTHER']))
        current = json.loads(json.dumps(original)); current['invoiceLines'][0]['description'] = 'Updated label'
        self.assertFalse(match_document(original, current)); self.assertTrue(match_document(original, current, safe_codes=['TEST']))
        self.assertEqual(leaf_codes({'errorCode': 'Err01', 'innerError': [{'errorCode': 'CF313'}]}), ['CF313'])

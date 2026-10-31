import hashlib
import json
import sqlite3
from contextlib import ExitStack, closing, contextmanager
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from odoo import Command, api, fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from ..models.client import EtaClient, EtaError
from ..models.historical_recovery import fiscal, fiscal_equal, leaf_codes, match_document
from ..models.catalogue_recovery import approval_identity, approved_usage


@tagged('post_install', '-at_install')
class TestHistoricalRecovery(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        company_partner = {'vat': '123456789'}
        for name, value in [('company_size', 'small'), ('company_device', 1)]:
            if name in cls.env['res.partner']._fields:
                company_partner[name] = value
        cls.company.partner_id.with_context(no_vat_validation=True).write(company_partner)
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

    def review(self, move, receipts=(), proof=True, match=True, outgoing=None):
        job = move.eta_job_ids
        job.write({'state': 'review', 'historical_review': True})
        client = MagicMock(); source = self.document(move)
        history = {'from': fields.Datetime.now() - timedelta(days=400), 'to': fields.Datetime.now() - timedelta(minutes=1), 'receipts': {}} if proof else None
        with ExitStack() as stack:
            stack.enter_context(patch.object(type(move), 'currency_id', self.env.ref('base.EGP')))
            stack.enter_context(patch.object(type(job), '_historical_source', return_value=source))
            stack.enter_context(patch.object(type(job), '_submission_source', return_value=outgoing or source))
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
        with patch.object(self, 'document', return_value=source), patch.object(type(job), '_catalogue_code_approved', return_value=False):
            job, _, _ = self.review(move, [raw])
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
            {'uuid': 'copy', 'internalId': move.name, 'status': 'Invalid', 'document': self.document(move), 'validationResults': {'errorCode': 'DS301', 'error': 'Duplicate UUID: ' + original}},
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

    def preflight(self, move, receipts=(), proof=True):
        job = move.eta_job_ids
        doc = self.document(move)
        job.payload = json.dumps({'documents': [doc]})
        history = {'from': fields.Datetime.now() - timedelta(days=400),
                   'to': fields.Datetime.now() - timedelta(minutes=1), 'receipts': {}} if proof else None
        stack = ExitStack()
        stack.enter_context(patch.object(type(job), '_historical_source', return_value=doc))
        stack.enter_context(patch.object(type(job), '_submission_source', return_value=doc))
        stack.enter_context(patch.object(type(job), '_history_snapshot', return_value=history))
        stack.enter_context(patch.object(type(job), '_history_candidates', return_value=list(receipts)))
        stack.enter_context(patch.object(type(job), '_historical_matches', return_value=True))
        stack.enter_context(patch.object(type(move), 'currency_id', self.env.ref('base.EGP')))
        return job, doc, stack

    def test_new_invoice_also_requires_complete_authority_proof_before_post(self):
        move = self.invoice(); job, doc, stack = self.preflight(move, proof=False)
        client = MagicMock()
        with stack, self.assertRaises(EtaError):
            job._submit(client)
        client.request.assert_not_called()
        self.assertFalse(job.attempted_at)

    def test_approval_arriving_after_historical_review_prevents_post(self):
        move = self.invoice(); doc = self.document(move)
        raw = {'uuid': 'late-approval', 'internalId': move.name, 'status': 'Valid', 'document': doc}
        job, _, stack = self.preflight(move, [raw])
        job.write({'historical_fingerprint': job._historical_financial_fingerprint(),
            'historical_submission_verified': True, 'historical_reviewed_at': fields.Datetime.now(),
            'authority_receipt_hash': job._receipt_set_hash([])})
        client = MagicMock()
        with stack:
            job._submit(client)
        self.assertEqual(job.state, 'valid'); self.assertEqual(move.e_invoice_uuid, 'late-approval')
        client.request.assert_not_called(); self.assertFalse(job.attempted_at)

    def test_new_invoice_with_complete_absence_posts_only_after_preflight(self):
        move = self.invoice(); job, doc, stack = self.preflight(move)
        client = MagicMock(); client.request.return_value = {
            'acceptedDocuments': [{'internalId': move.name, 'uuid': 'first-approval'}]}
        with stack, patch.object(self.env.cr, 'commit'):
            job._submit(client)
        self.assertEqual(job.state, 'accepted')
        self.assertEqual(client.request.call_args.args[:2], ('POST', '/api/v1/documentsubmissions'))

    def test_preflight_changed_invalid_set_reenters_review_without_post(self):
        move = self.invoice(); doc = self.document(move)
        raw = {'uuid': 'rejection', 'internalId': move.name, 'status': 'Invalid', 'document': doc,
               'validationResults': {'errorCode': 'NI310'}}
        job, _, stack = self.preflight(move, [raw]); client = MagicMock()
        with stack:
            job._submit(client)
        self.assertEqual(job.state, 'invalid'); self.assertFalse(job.attempted_at)
        client.request.assert_not_called()

    def test_preflight_known_rejection_proof_does_not_loop_forever(self):
        move = self.invoice(); old = self.document(move); old['dateTimeIssued'] = '2000-01-01T00:00:00Z'
        raw = {'uuid': 'date-rejection', 'internalId': move.name, 'status': 'Invalid', 'document': old,
               'validationResults': {'errorCode': 'CF313'}}
        job, doc, stack = self.preflight(move, [raw])
        job.write({'historical_submission_verified': True, 'authority_receipt_hash': job._receipt_set_hash([raw])})
        with stack:
            self.assertTrue(job._preflight_submission(MagicMock()))

    def test_receipt_from_another_taxpayer_cannot_mark_invoice_valid(self):
        move = self.invoice(); doc = self.document(move); doc['issuer']['id'] = '987654321'
        with self.assertRaisesRegex(UserError, 'issuer'):
            move.eta_job_ids._apply_remote({'uuid': 'wrong-taxpayer', 'internalId': move.name, 'status': 'Valid', 'document': doc})
        self.assertFalse(move.e_invoice_uuid)

    def test_ledger_change_after_durable_marker_retains_unknown_without_http(self):
        move = self.invoice(); job, _, stack = self.preflight(move); client = MagicMock()
        def changed():
            self.env.cr.execute('UPDATE account_move SET name=%s WHERE id=%s', ['CHANGED', move.id])
        # TransactionCase.assertRaises adds a savepoint rollback; use an
        # explicit catch to inspect the marker with the commit mocked here.
        with stack, patch.object(self.env.cr, 'commit', side_effect=changed):
            try:
                job._submit(client)
            except EtaError as caught:
                self.assertIn('Posted accounting changed before ETA transmission', str(caught))
            else:
                self.fail('A changed ledger identity reached ETA transmission')
        self.assertEqual(job.state, 'unknown'); self.assertTrue(job.attempted_at)
        client.request.assert_not_called()

    def test_legacy_sample_sender_never_posts_a_tax_document(self):
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Legacy test sent HTTP')):
            with self.assertRaisesRegex(UserError, 'sample-document'):
                self.company.test_call_e_invoice_api()

    def test_missing_custom_credentials_cannot_fall_back_to_native_sender(self):
        move = self.invoice(); self.company.client_secret = False
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Native sender sent HTTP')):
            result = self.env.ref('l10n_eg_edi_eta.edi_eg_eta')._l10n_eg_edi_post_invoice(move)
        self.assertEqual(result[move]['blocking_level'], 'error')
        self.assertIn('credentials', result[move]['error'])

    def test_catalogue_approval_is_exact_active_and_requester_scoped(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        publication = {'itemCode': 'EG-OTHER-ITEM', 'active': True}
        row = {'itemCode': 'EG-OTHER-ITEM', 'status': 'Approved', 'active': True,
               'requesterTaxpayer': {'rin': '123456789'}}
        self.assertTrue(approved_usage(publication, {'result': [row]}, 'EG-OTHER-ITEM', '123456789', now))
        for change in ({'status': 'Submitted'}, {'active': False}, {'requesterTaxpayer': {'rin': '987654321'}},
                       {'itemCode': 'EG-DIFFERENT'}, {'activeTo': '2000-01-01T00:00:00Z'}):
            self.assertFalse(approved_usage(publication, {'result': [dict(row, **change)]}, 'EG-OTHER-ITEM', '123456789', now))
        self.assertFalse(approved_usage(dict(publication, active=False), {'result': [row]}, 'EG-OTHER-ITEM', '123456789', now))
        proof = {'scope': 'uat|123456789|EGS|EG-OTHER-ITEM', 'publication': publication, 'usage': {'result': [row]}}
        unrelated = dict(row, itemCode='EG-UNRELATED', codeUsageRequestID=999)
        foreign = dict(row, requesterTaxpayer={'rin': '987654321'}, codeUsageRequestID=1000)
        self.assertEqual(approval_identity(proof), approval_identity(dict(proof, usage={'result': [row, unrelated, foreign]})))

    def test_approved_code_can_release_old_cv302_but_not_a_mixed_id_error(self):
        move = self.invoice(); doc = self.document(move)
        raw = {'uuid': 'code-rejection', 'internalId': move.name, 'status': 'Invalid', 'document': doc,
               'validationResults': {'errorCode': 'CV302'}}
        with patch.object(type(move.eta_job_ids), '_catalogue_code_approved', return_value=True):
            job, _, _ = self.review(move, [raw])
        self.assertEqual(job.state, 'queued'); self.assertTrue(job.authority_receipt_hash)
        raw['validationResults'] = {'details': [{'errorCode': 'CV302'}, {'errorCode': 'NI310'}]}
        with patch.object(type(job), '_catalogue_code_approved', return_value=True):
            job, _, _ = self.review(move, [raw])
        self.assertEqual(job.state, 'invalid')

    def test_unrelated_label_change_does_not_release_unchanged_bad_code(self):
        move = self.invoice(); old = self.document(move); old['invoiceLines'][0]['description'] = 'Old label'
        raw = {'uuid': 'bad-code', 'internalId': move.name, 'status': 'Invalid', 'document': old,
               'validationResults': {'errorCode': 'CV302'}}
        with patch.object(type(move.eta_job_ids), '_catalogue_code_approved', return_value=False):
            job, _, _ = self.review(move, [raw])
        self.assertEqual(job.state, 'invalid')

    def test_same_taxpayer_across_companies_shares_invoice_number_namespace(self):
        move = self.invoice(); egp = self.env.ref('base.EGP')
        def sibling(vat, code):
            values = {'name': 'Namespace sibling ' + code, 'is_company': True, 'vat': vat,
                      'country_id': self.env.ref('base.eg').id}
            for key, value in [('company_size', 'small'), ('company_device', 1)]:
                if key in self.env['res.partner']._fields:
                    values[key] = value
            partner = self.env['res.partner'].with_context(no_vat_validation=True).create(values)
            company = self.env['res.company'].create({'name': values['name'], 'partner_id': partner.id,
                'currency_id': egp.id, 'config_type': 'uat', 'client_id': 'other-client', 'client_secret': 'other-secret',
                'activity_code_id': self.company.activity_code_id.id, 'type_code_id': self.company.type_code_id.id,
                'sub_type_code_id': self.company.sub_type_code_id.id})
            receivable = self.env['account.account'].with_company(company).create({'name': 'Sibling receivable',
                'code': code + 'RCV', 'account_type': 'asset_receivable', 'reconcile': True,
                'company_ids': [Command.set(company.ids)]})
            income = self.env['account.account'].with_company(company).create({'name': 'Sibling income',
                'code': code + 'INC', 'account_type': 'income', 'company_ids': [Command.set(company.ids)]})
            self.partner.with_company(company).property_account_receivable_id = receivable
            journal = self.env['account.journal'].create({'name': 'Sibling sale', 'code': code, 'type': 'sale',
                'company_id': company.id, 'edi_format_ids': [Command.clear()]})
            invoice = self.env['account.move'].with_company(company).create({'move_type': 'out_invoice',
                'journal_id': journal.id, 'partner_id': self.partner.id, 'invoice_date': fields.Date.today(),
                'invoice_line_ids': [Command.create({'name': 'Sibling item', 'quantity': 1, 'price_unit': 100,
                    'account_id': income.id, 'tax_ids': [Command.clear()]})]})
            invoice.action_post()
            self.env.cr.execute('UPDATE account_move SET name=%s WHERE id=%s', [move.name, invoice.id])
            invoice.invalidate_recordset()
            return company
        other = sibling('123456789', 'SIBL')
        with self.assertRaisesRegex(UserError, 'across Odoo companies'):
            move.eta_job_ids._assert_eta_number_unique()
        other.config_type = 'prod'
        move.eta_job_ids._assert_eta_number_unique()
        # Create a different taxpayer correctly from the outset; enterprise
        # accounting rightly forbids changing a company's VAT after posting.
        sibling('987654321', 'DIFF')
        move.eta_job_ids._assert_eta_number_unique()

    def test_catalogue_approval_evidence_is_cached_per_taxpayer_and_preserved(self):
        move = self.invoice(); job = move.eta_job_ids; code = 'EG-OTHER-ITEM'
        publication = {'itemCode': code, 'active': True}
        usage = {'result': [{'itemCode': code, 'active': True, 'status': 'Approved',
                            'requesterTaxpayer': {'rin': '123456789'}}]}
        client = MagicMock()
        with patch.object(type(job), '_catalogue_get', side_effect=[publication, usage]) as get:
            self.assertTrue(job._catalogue_code_approved(client, code))
            self.assertTrue(job._catalogue_code_approved(client, code))
        self.assertEqual(get.call_count, 2)
        self.assertTrue(self.env['ir.attachment'].search_count([
            ('res_model', '=', 'account.move'), ('res_id', '=', move.id), ('name', 'like', 'ETA_catalogue_approval_%')]))
        with patch.object(type(job), '_catalogue_get', side_effect=EtaError('Missing', status=404)):
            self.assertFalse(job._catalogue_code_approved(client, 'EG-MISSING'))

    def test_catalogue_throttle_survives_job_rollback_and_defers_other_jobs(self):
        move = self.invoice(); job = move.eta_job_ids
        client = MagicMock(); client.api = 'https://catalogue-unit-test.invalid'
        key = 'tradeline_eta_reliability.catalogue_next_get.' + hashlib.sha256(client.api.encode()).hexdigest()
        real_cursor = self.env.registry._db.cursor
        def cleanup():
            with real_cursor() as cr:
                api.Environment(cr, self.env.uid, {})['ir.config_parameter'].sudo().search([('key', '=', key)]).unlink()
                cr.commit()
        cleanup(); self.addCleanup(cleanup)
        client.request.side_effect = EtaError('Throttled', status=429, retry_after=120)
        # Odoo normally proxies all test cursors onto one rollback connection.
        # Real separate transactions are essential to verify durable pacing.
        with patch.object(self.env.registry, 'cursor', side_effect=real_cursor), \
                patch('odoo.addons.tradeline_eta_reliability.models.catalogue_recovery.time.sleep'):
            with self.assertRaises(EtaError):
                job._catalogue_get(client, '/catalogue-unit-test')
            with real_cursor() as cr:
                cr.execute('SELECT value FROM ir_config_parameter WHERE key=%s', [key])
                row = cr.fetchone()
                self.assertTrue(row, 'The catalogue deadline was not committed independently')
                import time
                self.assertGreater(float(row[0]), time.time() + 60, 'Retry-After was not retained')
            with self.assertRaisesRegex(EtaError, 'cooldown'):
                job._catalogue_get(client, '/catalogue-unit-test')
        self.assertEqual(client.request.call_count, 1)

    def test_catalogue_rejection_after_approved_retry_cannot_loop_on_same_proof(self):
        move = self.invoice(); job = move.eta_job_ids; doc = self.document(move)
        publication = {'itemCode': 'TEST', 'active': True}
        row = {'itemCode': 'TEST', 'active': True, 'status': 'Approved', 'codeUsageRequestID': 1,
               'requesterTaxpayer': {'rin': '123456789'}}
        raw = {'uuid': 'old-rejection', 'internalId': move.name, 'status': 'Invalid', 'document': doc,
               'dateTimeReceived': '2026-03-01T00:00:00Z', 'validationResults': {'errorCode': 'CV302'}}
        with patch.object(type(job), '_catalogue_get', side_effect=[publication, {'result': [row]}]):
            job, _, _ = self.review(move, [raw])
        self.assertEqual(job.state, 'queued'); self.assertTrue(job.catalogue_evidence_hash)
        old_hash = job.catalogue_evidence_hash
        raw['dateTimeReceived'] = (fields.Datetime.now() + timedelta(seconds=1)).isoformat() + 'Z'
        job, _, _ = self.review(move, [raw])
        self.assertEqual(job.state, 'invalid'); self.assertEqual(job.catalogue_evidence_hash, old_hash)
        # A fresh read timestamp or label change is not a changed approval.
        proof = {'publication': publication, 'usage': {'result': [row]}}
        self.assertEqual(approval_identity(proof), approval_identity(dict(proof, captured_utc='2099-01-01')))
        _scope, key = job._catalogue_cache_key('TEST')
        self.env['ir.config_parameter'].sudo().search([('key', '=', key)]).unlink()
        changed = dict(row, codeUsageRequestID=2)
        with patch.object(type(job), '_catalogue_get', side_effect=[publication, {'result': [changed]}]):
            job, _, _ = self.review(move, [raw])
        self.assertEqual(job.state, 'queued'); self.assertNotEqual(job.catalogue_evidence_hash, old_hash)

    @contextmanager
    def personal_details_fields(self, partner):
        # Community QA lacks the hosted contact extension. Supply only its
        # absent address fields; exercise the real serializer on both hosts.
        with ExitStack() as stack:
            for name, value in [('mobile_type', 'local'), ('floor', False),
                                ('room', False), ('landmark', False), ('additional_info', False)]:
                if name not in partner._fields:
                    stack.enter_context(patch.object(type(partner), name, value, create=True))
            if 'mobile_type' in partner._fields and partner.mobile_type != 'local':
                partner.mobile_type = 'local'
            yield

    def test_preflight_accepts_authorized_optional_id_omission_without_editing_customer(self):
        move = self.invoice(); job = move.eta_job_ids
        move.partner_id.with_context(no_vat_validation=True).write({
            'vat': '12345678901234', 'country_id': self.env.ref('base.eg').id, 'city': 'Cairo', 'street': 'Test'})
        job.omit_optional_receiver_id = True
        before = (move.partner_id.vat, move.invoice_date, move.amount_total)
        def source(invoice):
            document = self.document(invoice)
            document['receiver'] = invoice._get_eta_personal_details(invoice.partner_id, invoice.amount_total)
            return document
        proof = {'from': fields.Datetime.now() - timedelta(days=400),
                 'to': fields.Datetime.now() - timedelta(minutes=1), 'receipts': {}}
        with self.personal_details_fields(move.partner_id), \
                patch.object(type(move), '_eta_correction_document', source), \
                patch.object(type(job), '_history_snapshot', return_value=proof), \
                patch.object(type(job), '_history_candidates', return_value=[]):
            outgoing = source(move)
            self.assertEqual(outgoing['receiver']['id'], '')
            self.assertEqual(job._historical_source()['receiver']['id'], before[0])
            job.payload = json.dumps({'documents': [outgoing]})
            self.assertTrue(job._preflight_submission(MagicMock()))
        self.assertEqual(before, (move.partner_id.vat, move.invoice_date, move.amount_total))

    def test_preflight_accepts_small_sale_placeholder_omission_from_existing_data(self):
        move = self.invoice(); job = move.eta_job_ids
        move.partner_id.country_id = self.env.ref('base.eg')
        if 'mobile_type' in move.partner_id._fields:
            move.partner_id.mobile_type = 'local'
        # Simulate the malformed legacy value seen in production without
        # weakening the hosted contact module's normal input validation.
        self.env.flush_all()
        self.env.cr.execute("UPDATE res_partner SET vat='1' WHERE id=%s", [move.partner_id.id])
        move.partner_id.invalidate_recordset()
        def source(invoice):
            document = self.document(invoice)
            document['receiver'] = invoice._get_eta_personal_details(invoice.partner_id, invoice.amount_total)
            return document
        proof = {'from': fields.Datetime.now() - timedelta(days=400),
                 'to': fields.Datetime.now() - timedelta(minutes=1), 'receipts': {}}
        with self.personal_details_fields(move.partner_id), \
                patch.object(type(move), '_eta_correction_document', source), \
                patch.object(type(job), '_history_snapshot', return_value=proof), \
                patch.object(type(job), '_history_candidates', return_value=[]):
            outgoing = source(move)
            self.assertEqual(outgoing['receiver']['id'], '')
            job.payload = json.dumps({'documents': [outgoing]})
            self.assertTrue(job._preflight_submission(MagicMock()))
        self.assertEqual(move.partner_id.vat, '1')

    def test_mixed_id_and_catalogue_rejection_requires_both_corrections(self):
        for approved in (False, True):
            move = self.invoice(); job = move.eta_job_ids
            source = self.document(move); source['receiver']['id'] = '123456789012345'
            raw = {'uuid': 'mixed-id-code', 'internalId': move.name, 'status': 'Invalid', 'document': source,
                'validationResults': {'details': [{'errorCode': 'NI310'}, {'errorCode': 'SF307'}, {'errorCode': 'CV302'}]}}
            before = (move.invoice_date, move.amount_total, move.partner_id.vat)
            with patch.object(self, 'document', return_value=source), \
                    patch.object(type(job), '_catalogue_code_approved', return_value=approved):
                job, client, _ = self.review(move, [raw])
            self.assertEqual(job.state, 'queued' if approved else 'invalid')
            self.assertEqual(bool(job.omit_optional_receiver_id), approved)
            self.assertEqual(before, (move.invoice_date, move.amount_total, move.partner_id.vat))
            client.request.assert_not_called()

    def test_new_empty_id_rejection_cannot_retry_using_older_nonempty_id(self):
        move = self.invoice(); job = move.eta_job_ids
        source = self.document(move); source['receiver']['id'] = '123456789012345'
        outgoing = json.loads(json.dumps(source)); outgoing['receiver']['id'] = ''
        receipts = [{'uuid': 'older-id', 'internalId': move.name, 'status': 'Invalid', 'document': source,
                     'validationResults': {'errorCode': 'NI310'}},
                    {'uuid': 'newer-empty', 'internalId': move.name, 'status': 'Invalid', 'document': outgoing,
                     'validationResults': {'errorCode': 'CF305'}}]
        with patch.object(self, 'document', return_value=source):
            job, client, _ = self.review(move, receipts, outgoing=outgoing)
        self.assertEqual(job.state, 'invalid'); self.assertFalse(job.historical_submission_verified)
        self.assertIn('CF305', job.last_error); client.request.assert_not_called()

    def test_mixed_rejection_never_omits_mandatory_id(self):
        move = self.invoice(); move.button_draft(); move.invoice_line_ids.price_unit = 25000; move.action_post()
        job = move.eta_job_ids; source = self.document(move)
        source.update(totalAmount=25000, netAmount=25000); source['receiver']['id'] = '123456789012345'
        raw = {'uuid': 'required-id', 'internalId': move.name, 'status': 'Invalid', 'document': source,
            'validationResults': {'details': [{'errorCode': 'NI310'}, {'errorCode': 'CV302'}]}}
        with patch.object(self, 'document', return_value=source), \
                patch.object(type(job), '_catalogue_code_approved', return_value=True):
            job, client, _ = self.review(move, [raw])
        self.assertEqual(job.state, 'invalid'); self.assertFalse(job.omit_optional_receiver_id)
        client.request.assert_not_called()

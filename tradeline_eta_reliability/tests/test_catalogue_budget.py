import hashlib
import json
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from odoo import Command, api, fields
from odoo.tests import TransactionCase, tagged

from ..models.client import EtaError, EtaCataloguePending
from ..models.catalogue_recovery import CATALOGUE_CONTINUATION


@tagged('post_install', '-at_install')
class TestCatalogueBudget(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        values = {'vat': '123456789'}
        for name, value in [('company_size', 'small'), ('company_device', 1)]:
            if name in cls.env['res.partner']._fields:
                values[name] = value
        cls.company.partner_id.with_context(no_vat_validation=True).write(values)
        cls.company.config_type = 'uat'
        cls.receivable = cls.env['account.account'].create({'name': 'Catalogue budget receivable', 'code': 'CBUDAR',
            'account_type': 'asset_receivable', 'reconcile': True, 'company_ids': [Command.set(cls.company.ids)]})
        cls.income = cls.env['account.account'].create({'name': 'Catalogue budget income', 'code': 'CBUDINC',
            'account_type': 'income', 'company_ids': [Command.set(cls.company.ids)]})
        cls.partner = cls.env['res.partner'].create({'name': 'Catalogue budget QA buyer',
            'property_account_receivable_id': cls.receivable.id})
        cls.journal = cls.env['account.journal'].create({'name': 'Catalogue budget sales', 'code': 'CBUD',
            'type': 'sale', 'company_id': cls.company.id, 'edi_format_ids': [Command.clear()]})

    def setUp(self):
        super().setUp()
        self.env['ir.config_parameter'].sudo().set_param('tradeline_eta_reliability.enabled_database', self.env.cr.dbname)
        self.env.ref('tradeline_eta_reliability.cron_eta_queue').active = True
        self.job = self.make_job()
        self.code = 'QA-BUDGET-' + uuid.uuid4().hex
        self.keys = set()
        self.addCleanup(self.clear_committed_test_keys)
        guard = patch('requests.sessions.Session.request', side_effect=AssertionError('Unexpected external HTTP'))
        guard.start(); self.addCleanup(guard.stop)

    def make_job(self, state='review'):
        move = self.env['account.move'].create({'move_type': 'out_invoice', 'journal_id': self.journal.id,
            'partner_id': self.partner.id, 'invoice_date': fields.Date.today(),
            'invoice_line_ids': [Command.create({'name': 'Catalogue budget QA item', 'account_id': self.income.id,
                'quantity': 1, 'price_unit': 100, 'tax_ids': [Command.clear()]})]})
        move.action_post()
        job = move.eta_job_ids
        job.write({'state': state, 'historical_fingerprint': 'qa-only-baseline', 'next_attempt': False})
        return job

    def key(self, code=None):
        scope, key = self.job._catalogue_cache_key(code or self.code)
        self.keys.update([key, key.replace('.catalogue_proof.', '.catalogue_partial.')])
        return scope, key

    def clear_committed_test_keys(self):
        # Exact, random QA keys only; the invoice and its fixtures roll back.
        if self.keys:
            with self.env.registry.cursor() as cr:
                cr.execute('DELETE FROM ir_config_parameter WHERE key IN %s', [tuple(self.keys)])
                cr.commit()
            self.env.registry.clear_cache()

    @contextmanager
    def pipeline(self, *, native=True, response=None):
        clock = [time.time() + 100]
        client = SimpleNamespace(api='https://catalogue-qa.invalid/' + uuid.uuid4().hex, request=MagicMock())
        rate = 'tradeline_eta_reliability.catalogue_next_get.' + hashlib.sha256(client.api.encode()).hexdigest()
        self.keys.add(rate)
        calls = []
        def request(method, path, **kwargs):
            self.assertEqual(method, 'GET')
            calls.append((clock[0], path))
            if response:
                return response(method, path, **kwargs)
            if '/codetypes/requests/' in path:
                code = kwargs['params']['ItemCode']
                return {'result': [{'itemCode': code, 'active': True, 'status': 'Approved',
                    'requesterTaxpayer': {'rin': '123456789'}, 'codeUsageRequestID': 'qa-' + code}]}
            return {'itemCode': path.rsplit('/', 1)[-1], 'active': True}
        client.request.side_effect = request
        with patch('odoo.addons.tradeline_eta_reliability.models.catalogue_recovery.time.time', side_effect=lambda: clock[0]), \
                patch('odoo.addons.tradeline_eta_reliability.models.catalogue_recovery.time.sleep') as sleep, \
                patch('odoo.addons.tradeline_eta_reliability.models.catalogue_recovery.check_processing_window'):
            yield SimpleNamespace(client=client, clock=clock, calls=calls, sleep=sleep,
                job=self.job.with_context(eta_catalogue_nonblocking=native), rate=rate)

    def marker(self, target=None, phase='usage', **overrides):
        job = target or self.job
        now = fields.Datetime.now()
        data = {'database': self.env.cr.dbname, 'job': job.id, 'company': job.company_id.id,
            'requested_at': fields.Datetime.to_string(now-timedelta(seconds=20)),
            'not_before': fields.Datetime.to_string(now-timedelta(seconds=5)),
            'expires_at': fields.Datetime.to_string(now+timedelta(minutes=9)), 'phase': phase}
        data.update(overrides)
        return self.env['ir.attachment'].sudo().create({'name': CATALOGUE_CONTINUATION,
            'res_model': job._name, 'res_id': job.id, 'company_id': job.company_id.id,
            'public': False, 'mimetype': 'application/json', 'raw': json.dumps(data).encode()})

    def test_publication_is_not_approval_and_resume_does_not_repeat_it_or_sleep(self):
        scope, key = self.key()
        with self.pipeline() as run:
            with self.assertRaises(EtaCataloguePending) as raised:
                run.job._catalogue_code_approved(run.client, self.code)
            self.assertEqual(raised.exception.phase, 'usage')
            self.assertIsNone(self.job.with_context(eta_catalogue_nonblocking=True)._catalogue_read_evidence(key))
            partial = self.job.with_context(eta_catalogue_nonblocking=True)._catalogue_read_evidence(key.replace('.catalogue_proof.', '.catalogue_partial.'))
            self.assertEqual(partial['scope'], scope)
            self.assertNotIn('approved', partial)
            self.assertEqual(len(run.calls), 1)
            run.clock[0] += 11
            self.assertTrue(run.job._catalogue_code_approved(run.client, self.code))
            self.assertEqual(len(run.calls), 2)
            proof = self.job.with_context(eta_catalogue_nonblocking=True)._catalogue_read_evidence(key)
            self.assertTrue(proof['approved'])
            self.assertEqual(proof['captured_utc'], partial['captured_utc'])
            self.assertTrue(all(c.args == (0,) for c in run.sleep.call_args_list))
            self.assertGreaterEqual(run.calls[1][0]-run.calls[0][0], 10.1)

    def test_shared_evidence_survives_invoice_savepoint_rollback_but_not_source_writes(self):
        _scope, key = self.key()
        class RollbackPreview(Exception): pass
        with self.pipeline() as run:
            with self.assertRaises(RollbackPreview):
                with self.env.cr.savepoint():
                    self.job.move_id.ref = 'this change must roll back'
                    with self.assertRaises(EtaCataloguePending):
                        run.job._catalogue_code_approved(run.client, self.code)
                    raise RollbackPreview()
            self.job.move_id.invalidate_recordset()
            self.assertFalse(self.job.move_id.ref)
            self.assertTrue(self.job.with_context(eta_catalogue_nonblocking=True)._catalogue_read_evidence(key.replace('.catalogue_proof.', '.catalogue_partial.')))
            run.clock[0] += 11
            with self.assertRaises(RollbackPreview):
                with self.env.cr.savepoint():
                    self.assertTrue(run.job._catalogue_code_approved(run.client, self.code))
                    raise RollbackPreview()
            self.assertTrue(self.job.with_context(eta_catalogue_nonblocking=True)._catalogue_read_evidence(key)['approved'])

    def test_every_code_must_complete_and_each_step_rechecks_the_rejection(self):
        codes = [self.code+'A', self.code+'B']
        for code in codes: self.key(code)
        document = {'invoiceLines': [{'itemType': 'EGS', 'itemCode': code} for code in codes]}
        receipt = {'dateTimeReceived': (datetime.now(timezone.utc)-timedelta(days=1)).isoformat()}
        with self.pipeline() as run:
            for phase in ('usage', 'publication', 'usage'):
                with self.assertRaises(EtaCataloguePending) as raised:
                    run.job._catalogue_rejection_resolved(run.client, document, document, receipt)
                self.assertEqual(raised.exception.phase, phase)
                self.assertFalse(self.job.catalogue_evidence_hash)
                run.clock[0] += 11
            self.assertTrue(run.job._catalogue_rejection_resolved(run.client, document, document, receipt))
            self.assertEqual(len(run.calls), 4)
            for first, second in zip(run.calls, run.calls[1:]): self.assertGreaterEqual(second[0]-first[0], 10.1)
            receipt['dateTimeReceived'] = (datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat()
            self.assertFalse(run.job._catalogue_rejection_resolved(run.client, document, document, receipt),
                'The same approval cannot authorize retrying its newer CV302 rejection')

    def test_invalid_partial_scope_database_future_expired_or_empty_is_never_reused(self):
        scope, key = self.key(); partial_key = key.replace('.catalogue_proof.', '.catalogue_partial.')
        good = {'database': self.env.cr.dbname, 'scope': scope,
            'captured_utc': datetime.now(timezone.utc).isoformat(), 'publication': {'itemCode': self.code, 'active': True}}
        variants = [dict(good, database='another-database'), dict(good, scope='wrong-scope'),
            dict(good, captured_utc=(datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat()),
            dict(good, captured_utc=(datetime.now(timezone.utc)-timedelta(minutes=6)).isoformat()),
            dict(good, publication={})]
        for bad in variants:
            with self.subTest(proof=bad), self.pipeline() as run:
                self.job._catalogue_store_evidence(partial_key, bad)
                with self.assertRaises(EtaCataloguePending): run.job._catalogue_code_approved(run.client, self.code)
                self.assertIn('/codes/', run.calls[0][1])
                self.assertIsNone(self.job.with_context(eta_catalogue_nonblocking=True)._catalogue_read_evidence(key))

    def test_404_is_a_real_negative_publication_not_a_pending_or_success(self):
        _scope, key = self.key()
        def missing(*args, **kwargs): raise EtaError('Not found', status=404)
        with self.pipeline(response=missing) as run:
            self.assertFalse(run.job._catalogue_code_approved(run.client, self.code))
            self.assertEqual(len(run.calls), 1)
            self.assertFalse(self.job.with_context(eta_catalogue_nonblocking=True)._catalogue_read_evidence(key)['approved'])

    def test_429_and_503_cooldowns_remain_real_failures_and_survive_rollback(self):
        for status in (429, 503):
            with self.subTest(status=status), self.pipeline(response=lambda *a, **kw: (_ for _ in ()).throw(
                    EtaError('Authority cooldown', status=status, retry_after=91))) as run:
                with self.assertRaises(EtaError) as first: run.job._catalogue_get(run.client, '/codes/test')
                self.assertEqual(first.exception.status, status)
                run.clock[0] += 11
                with self.assertRaises(EtaError) as second: run.job._catalogue_get(run.client, '/codes/test')
                self.assertNotIsInstance(second.exception, EtaCataloguePending)
                self.assertGreaterEqual(second.exception.retry_after, 80)
                self.assertEqual(len(run.calls), 1)

    def test_explicit_proof_callers_keep_the_existing_blocking_gate(self):
        self.key()
        with self.pipeline(native=False) as run:
            self.assertTrue(run.job._catalogue_code_approved(run.client, self.code))
            self.assertEqual(len(run.calls), 2)
            self.assertTrue(any(c.args[0] > 10 for c in run.sleep.call_args_list))

    def test_inactive_or_wrong_taxpayer_usage_does_not_approve_a_partial(self):
        self.key()
        def response(_method, path, **kwargs):
            if '/requests/' not in path: return {'itemCode': self.code, 'active': True}
            return {'result': [{'itemCode': self.code, 'active': True, 'status': 'Approved',
                                'requesterTaxpayer': {'rin': '999999999'}}]}
        with self.pipeline(response=response) as run:
            with self.assertRaises(EtaCataloguePending): run.job._catalogue_code_approved(run.client, self.code)
            run.clock[0] += 11
            self.assertFalse(run.job._catalogue_code_approved(run.client, self.code))

    def test_future_retry_uncertainty_payload_and_rejection_are_preserved_on_defer(self):
        self.job.write({'state': 'unknown', 'attempts': 1, 'attempted_at': fields.Datetime.now(),
            'payload': 'retained signed bytes', 'payload_hash': 'retained hash', 'failures': 3,
            'duplicate_retry_at': fields.Datetime.now()+timedelta(minutes=15), 'duplicate_payload_hash': 'original duplicate hash',
            'last_error': 'Submission outcome needs reconciliation. No duplicate was sent.',
            'next_attempt': fields.Datetime.now()+timedelta(minutes=20)})
        names = ['state','attempts','attempted_at','payload','payload_hash','failures','duplicate_retry_at','duplicate_payload_hash','last_error','next_attempt']
        before = [self.job[name] for name in names]
        self.job._defer_catalogue_lookup(EtaCataloguePending(9, 'usage'))
        self.assertEqual(before, [self.job[name] for name in names])
        self.assertFalse(self.job.needs_attention, 'A documented future duplicate cooldown is not a new uncertainty alarm')
        self.job.duplicate_retry_at = False
        self.assertTrue(self.job.needs_attention, 'A genuinely uncertain result remains visible')

    def test_normal_pacing_adds_no_failure_or_attention_alarm_and_retains_finance_error(self):
        self.job.write({'last_error': False, 'failures': 0})
        self.job._defer_catalogue_lookup(EtaCataloguePending(9, 'usage'))
        self.assertEqual(self.job.failures, 0)
        self.assertFalse(self.job.needs_attention)
        error = 'ETA rejected the submission (2). {"errorCode":"CV302"}'
        self.job.write({'last_error': error, 'safe_to_retry': True})
        self.job._defer_catalogue_lookup(EtaCataloguePending(9, 'publication'))
        self.assertEqual(self.job.last_error, error)
        self.assertTrue(self.job.needs_attention)

    def test_queued_deferral_returns_to_full_history_review_without_touching_source(self):
        self.job.state = 'queued'; self.job.historical_submission_verified = True
        before = (self.job.move_id.invoice_date, self.job.move_id.date, self.job.move_id.amount_total,
                  self.job.move_id.e_invoice_json, self.job.payload, self.job.attempts)
        self.job._defer_catalogue_lookup(EtaCataloguePending(9, 'usage'))
        self.assertEqual(self.job.state, 'review')
        self.assertTrue(self.job.historical_review)
        self.assertFalse(self.job.historical_submission_verified)
        self.assertEqual(before, (self.job.move_id.invoice_date, self.job.move_id.date, self.job.move_id.amount_total,
                  self.job.move_id.e_invoice_json, self.job.payload, self.job.attempts))

    def test_continuation_can_resume_beyond_the_ordinary_review_page(self):
        self.marker()
        with patch.object(type(self.job), '_due_jobs', return_value=[]):
            self.assertEqual(self.job._native_batch_candidate('all', 1), self.job)

    def test_usage_continuation_finishes_before_opening_more_publications(self):
        other = self.make_job()
        self.marker(phase='publication'); self.marker(other, phase='usage')
        self.assertEqual(self.job._catalogue_continuation_candidate('all'), other)

    def test_fresh_uncertain_stale_validation_and_verified_history_preempt_continuation(self):
        self.marker()
        now = fields.Datetime.now()
        for state, fingerprint, checked, reviewed in [('queued', False, False, False),
                ('unknown', 'baseline', False, False), ('accepted', 'baseline', now-timedelta(minutes=6), False),
                ('queued', 'baseline', False, now)]:
            with self.subTest(state=state, fingerprint=fingerprint):
                candidate = SimpleNamespace(id=999, state=state, historical_fingerprint=fingerprint,
                    historical_reviewed_at=reviewed, checked_at=checked, create_date=now)
                with patch.object(type(self.job), '_due_jobs', return_value=[candidate]):
                    self.assertIs(self.job._native_batch_candidate('all', 1), candidate)

    def test_wrong_scope_future_expired_public_or_malformed_continuations_are_ignored(self):
        now = fields.Datetime.now()
        variants = [{'database':'other'}, {'company':-1}, {'job':self.job.id+1}, {'phase':'invalid'},
            {'not_before':fields.Datetime.to_string(now+timedelta(minutes=1))},
            {'expires_at':fields.Datetime.to_string(now-timedelta(seconds=1))}]
        for data in variants:
            marker = self.marker(**data)
            self.assertFalse(self.job._catalogue_continuation_candidate('all'))
            marker.unlink()
        marker = self.marker(); marker.public = True
        self.assertFalse(self.job._catalogue_continuation_candidate('all'))
        marker.public = False; marker.raw = b'invalid json'
        self.assertFalse(self.job._catalogue_continuation_candidate('all'))

    def test_next_attempt_future_and_completed_states_never_resume_from_a_marker(self):
        self.marker()
        self.job.next_attempt = fields.Datetime.now()+timedelta(minutes=2)
        self.assertFalse(self.job._catalogue_continuation_candidate('all'))
        self.job.next_attempt = False
        for state in ('valid','accepted','invalid','blocked','unknown'):
            self.job.state = state
            self.assertFalse(self.job._catalogue_continuation_candidate('all'))
        self.job.state = 'review'
        self.assertFalse(self.job._catalogue_continuation_candidate('submit'))
        self.assertFalse(self.job._catalogue_continuation_candidate('status'))



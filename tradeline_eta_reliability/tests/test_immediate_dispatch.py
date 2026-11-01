import json
from contextlib import ExitStack, contextmanager
from datetime import timedelta
from functools import partial
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user

from ..models.immediate_dispatch import PRIORITY_REQUEST_NAME, dispatch_after_commit
from ..models.processing_window import EtaStopped

MODULE = 'odoo.addons.tradeline_eta_reliability.models.immediate_dispatch'


@tagged('post_install', '-at_install')
class TestImmediateDispatch(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        company_partner = {'vat': '123456789'}
        for name, value in (('company_size', 'small'), ('company_device', 1)):
            if name in cls.env['res.partner']._fields:
                company_partner[name] = value
        cls.company.partner_id.with_context(no_vat_validation=True).write(company_partner)
        cls.env.ref('base.EGP').active = True
        cls.company.write({'client_id': 'immediate-qa', 'client_secret': 'qa-only-secret',
            'config_type': 'uat', 'config_version': '1.0', 'signature_tool': 'python'})
        cls.receivable = cls.env['account.account'].create({'name': 'Immediate QA receivable', 'code': 'IDSPAR',
            'account_type': 'asset_receivable', 'reconcile': True, 'company_ids': [Command.set(cls.company.ids)]})
        cls.income = cls.env['account.account'].create({'name': 'Immediate QA income', 'code': 'IDSPINC',
            'account_type': 'income', 'company_ids': [Command.set(cls.company.ids)]})
        cls.partner = cls.env['res.partner'].create({'name': 'Immediate QA buyer',
            'property_account_receivable_id': cls.receivable.id})
        cls.journal = cls.env['account.journal'].create({'name': 'Immediate QA sales', 'code': 'IDSP',
            'type': 'sale', 'company_id': cls.company.id, 'edi_format_ids': [Command.clear()]})

    def setUp(self):
        super().setUp()
        self.Job = self.env['tradeline.eta.job']
        self.cron = self.env.ref('tradeline_eta_reliability.cron_eta_queue')
        self.cron.write({'active': True, 'nextcall': fields.Datetime.now() + timedelta(minutes=10)})
        self.env['ir.config_parameter'].sudo().set_param('tradeline_eta_reliability.enabled_database', self.env.cr.dbname)
        # Hosted stage and the reused local QA DB can contain old pending jobs.
        # Restrict ORM queue selection to this fixture journal without editing
        # those business records, even temporarily.
        search, count = self.Job.search, self.Job.search_count
        for name, original in (('search', search), ('search_count', count)):
            scoped = patch.object(type(self.Job), name, side_effect=lambda domain, *args, _original=original, **kwargs:
                _original(domain + [('move_id.journal_id', '=', self.journal.id)], *args, **kwargs))
            scoped.start(); self.addCleanup(scoped.stop)
        guard = patch('requests.sessions.Session.request', side_effect=AssertionError('Immediate QA attempted real HTTP'))
        guard.start(); self.addCleanup(guard.stop)
        callback = patch(MODULE + '.dispatch_after_commit')
        self.callback = callback.start(); self.addCleanup(callback.stop)

    def invoice(self):
        move = self.env['account.move'].create({'move_type': 'out_invoice', 'journal_id': self.journal.id,
            'currency_id': self.env.ref('base.EGP').id, 'partner_id': self.partner.id,
            'invoice_date': fields.Date.today(), 'invoice_line_ids': [Command.create({
                'name': 'Immediate QA item', 'account_id': self.income.id, 'quantity': 1,
                'price_unit': 100, 'tax_ids': [Command.clear()]})]})
        move.action_post()
        return move

    def requests(self, job):
        return self.env['ir.attachment'].sudo().search([
            ('res_model', '=', 'tradeline.eta.job'), ('res_id', '=', job.id), ('name', '=', PRIORITY_REQUEST_NAME)])

    @contextmanager
    def dispatch_harness(self, processor=None, seconds=0):
        calls = []; clock = [0.0]
        def process(job_id, client):
            calls.append(job_id); clock[0] += seconds
            job = self.Job.browse(job_id)
            if processor:
                return processor(job, client)
            job.write({'state': 'accepted', 'next_attempt': fields.Datetime.now() + timedelta(minutes=5)})
            return True
        with ExitStack() as stack:
            stack.enter_context(patch.object(self.env.cr, 'commit'))
            stack.enter_context(patch.object(self.env.cr, 'rollback'))
            stack.enter_context(patch(MODULE + '.time.monotonic', side_effect=lambda: clock[0]))
            client = stack.enter_context(patch(MODULE + '.EtaClient'))
            process_mock = stack.enter_context(patch.object(type(self.Job), '_process_id', side_effect=process))
            yield SimpleNamespace(calls=calls, client=client, process=process_mock)

    def test_new_posting_registers_dispatch_after_commit_and_one_native_wakeup(self):
        callbacks = []
        before = self.env['ir.cron.trigger'].search_count([('cron_id', '=', self.cron.id)])
        with patch.object(type(self.env.cr.postcommit), 'add', autospec=True,
                side_effect=lambda owner, callback: callbacks.append(callback)):
            move = self.invoice()
        self.assertFalse(self.callback.called)
        dispatch = [c for c in callbacks if isinstance(c, partial) and c.func is self.callback]
        self.assertEqual(len(dispatch), 1)
        notify = next(c for c in callbacks if getattr(c, '__name__', '') == '_notifydb'
            and getattr(getattr(c, '__self__', None), 'ids', []) == self.cron.ids)
        self.assertLess(callbacks.index(dispatch[0]), callbacks.index(notify))
        self.assertEqual(dispatch[0].args[2], tuple(move.eta_job_ids.ids))
        self.assertEqual(self.env['ir.cron.trigger'].search_count([('cron_id', '=', self.cron.id)]) - before, 1)
        dispatch[0]()
        self.callback.assert_called_once_with(self.env.registry, self.env.uid, tuple(move.eta_job_ids.ids))
        self.assertFalse(self.requests(move.eta_job_ids), 'Fresh jobs already have durable queue priority')

    def test_repeated_sync_reuses_one_job_and_one_private_priority_request(self):
        move = self.invoice(); job = move.eta_job_ids
        before = (move.invoice_date, move.amount_total, move.partner_id.vat)
        move.action_send_electronic_invoice(); move.action_send_electronic_invoice()
        self.assertEqual(move.eta_job_ids, job)
        self.assertEqual(len(self.requests(job)), 1)
        self.assertFalse(self.requests(job).public)
        self.assertEqual((job.state, job.attempts), ('queued', 0))
        self.assertEqual(before, (move.invoice_date, move.amount_total, move.partner_id.vat))

    def test_dispatch_processes_same_job_once_then_respects_accepted_poll_deadline(self):
        job = self.invoice().eta_job_ids
        with self.dispatch_harness() as run:
            self.assertEqual(job._dispatch_immediately(), 1)
            self.assertEqual(job._dispatch_immediately(), 0)
        self.assertEqual(run.calls, job.ids)
        run.client.return_value.close.assert_called_once()

    def test_unknown_is_reconciled_then_submitted_only_by_normal_process_id(self):
        job = self.invoice().eta_job_ids
        job.write({'state': 'unknown', 'first_submission_candidate': True})
        transitions = []
        def process(row, client):
            transitions.append(row.state)
            row.write({'state': 'queued'} if row.state == 'unknown' else {
                'state': 'accepted', 'next_attempt': fields.Datetime.now() + timedelta(minutes=5)})
            return True
        with self.dispatch_harness(processor=process) as run:
            self.assertEqual(job._dispatch_immediately(), 2)
        self.assertEqual(transitions, ['unknown', 'queued'])
        self.assertEqual(run.calls, job.ids * 2)

    def test_genuine_uncertainty_and_retry_after_are_never_cleared_by_sync(self):
        move = self.invoice(); job = move.eta_job_ids
        deadline = fields.Datetime.now() + timedelta(minutes=20)
        job.write({'state': 'unknown', 'attempted_at': fields.Datetime.now(), 'attempts': 1,
            'payload': 'retained request', 'payload_hash': 'retained hash', 'duplicate_retry_at': deadline,
            'duplicate_payload_hash': 'retained hash', 'next_attempt': deadline, 'last_error': 'ETA cooldown'})
        before = job.read(['state', 'attempted_at', 'attempts', 'payload', 'payload_hash',
            'duplicate_retry_at', 'duplicate_payload_hash', 'next_attempt', 'last_error'])
        move.action_send_electronic_invoice()
        with self.dispatch_harness() as run:
            self.assertEqual(job._dispatch_immediately(), 0)
        self.assertFalse(run.calls)
        self.assertEqual(before, job.read(list(before[0].keys())))
        self.assertFalse(self.Job._priority_dispatch_candidate('all'))

    def test_busy_transport_keeps_priority_and_does_not_construct_a_client(self):
        job = self.invoice().eta_job_ids
        job._request_priority_dispatch()
        with self.env.registry.cursor() as other:
            other.execute('SELECT pg_advisory_lock(741181,1)')
            try:
                with self.dispatch_harness() as run:
                    self.assertEqual(job._dispatch_immediately(), 0)
                run.client.assert_not_called()
                self.assertEqual(len(self.requests(job)), 1)
            finally:
                other.execute('SELECT pg_advisory_unlock(741181,1)')

    def test_noop_job_lock_result_never_spins_or_claims_progress(self):
        job = self.invoice().eta_job_ids
        with self.dispatch_harness(processor=lambda job, client: False) as run:
            self.assertEqual(job._dispatch_immediately(), 0)
        self.assertEqual(run.calls, job.ids)

    def test_short_dispatch_yields_after_two_operations(self):
        jobs = self.Job.browse()
        for _i in range(4):jobs |= self.invoice().eta_job_ids
        with self.dispatch_harness() as run:
            self.assertEqual(jobs._dispatch_immediately(), 2)
        self.assertEqual(run.calls, jobs.ids[:2])
        self.assertEqual(len(jobs.filtered(lambda j: j.state == 'queued')), 2)

    def test_soft_budget_does_not_start_a_second_operation(self):
        jobs = self.invoice().eta_job_ids | self.invoice().eta_job_ids
        with self.dispatch_harness(seconds=9) as run:
            self.assertEqual(jobs._dispatch_immediately(), 1)
        self.assertEqual(run.calls, jobs.ids[:1])

    def test_rolled_back_posting_is_not_visible_to_dispatch(self):
        ids = []
        with self.assertRaisesRegex(UserError, 'rollback'):
            with self.env.cr.savepoint():
                ids = self.invoice().eta_job_ids.ids
                raise UserError('rollback')
        with self.dispatch_harness() as run:
            self.assertEqual(self.Job.browse(ids)._dispatch_immediately(), 0)
        self.assertFalse(run.calls); run.client.assert_not_called()

    def test_priority_request_rolls_back_with_the_click(self):
        job = self.invoice().eta_job_ids
        with self.assertRaisesRegex(UserError, 'rollback'):
            with self.env.cr.savepoint():
                job._request_priority_dispatch()
                self.assertTrue(self.requests(job))
                raise UserError('rollback')
        self.assertFalse(self.requests(job))

    def test_terminal_valid_document_cannot_be_resubmitted(self):
        move = self.invoice(); job = move.eta_job_ids
        move.write({'e_invoice_uuid': 'existing-valid', 'e_invoice_status': 'Valid'})
        job.state = 'valid'
        move.action_send_electronic_invoice()
        with self.dispatch_harness() as run:
            self.assertEqual(job._dispatch_immediately(), 0)
        self.assertFalse(run.calls); self.assertFalse(self.requests(job))
        self.assertEqual(move.e_invoice_uuid, 'existing-valid')

    def test_disabled_cron_wrong_database_and_pause_never_dispatch(self):
        job = self.invoice().eta_job_ids
        self.cron.active = False
        with self.dispatch_harness() as run:self.assertEqual(job._dispatch_immediately(), 0)
        self.assertFalse(run.calls)
        self.cron.active = True
        self.env['ir.config_parameter'].sudo().set_param('tradeline_eta_reliability.enabled_database', 'copied-stage')
        self.assertFalse(job._request_priority_dispatch())
        with self.dispatch_harness() as run:self.assertEqual(job._dispatch_immediately(), 0)
        self.assertFalse(run.calls)
        self.env['ir.config_parameter'].sudo().set_param('tradeline_eta_reliability.enabled_database', self.env.cr.dbname)
        with patch(MODULE + '.check_processing_window', side_effect=EtaStopped('Owner pause')):
            self.assertFalse(job._request_priority_dispatch())
            with self.dispatch_harness() as run:self.assertEqual(job._dispatch_immediately(), 0)
        self.assertFalse(run.calls); self.assertFalse(self.requests(job))

    def test_manual_history_request_does_not_delay_a_fresh_invoice(self):
        old = self.invoice().eta_job_ids
        old.write({'state': 'review', 'historical_review': True, 'historical_fingerprint': 'saved baseline'})
        old._request_priority_dispatch()
        fresh = self.invoice().eta_job_ids
        with self.dispatch_harness() as run:self.assertEqual(old._dispatch_immediately(), 0)
        self.assertFalse(run.calls)
        self.assertEqual(self.Job._native_batch_candidate('all', 0), fresh)

    def test_manual_history_request_is_selected_outside_the_normal_review_page(self):
        jobs = self.Job.browse()
        for _i in range(24):jobs |= self.invoice().eta_job_ids
        jobs.write({'state': 'review', 'historical_review': True, 'historical_fingerprint': 'saved baseline'})
        target = jobs[-1:]
        self.assertNotIn(target.id, [j.id for j in self.Job._due_jobs('all')])
        target._request_priority_dispatch()
        self.assertEqual(self.Job._native_batch_candidate('all', 0), target)

    def test_completed_requests_cannot_hide_a_pending_request_before_limit(self):
        done = self.invoice().eta_job_ids; done.state = 'valid'
        values = {'name': PRIORITY_REQUEST_NAME, 'res_model': self.Job._name, 'res_id': done.id,
            'company_id': done.company_id.id, 'public': False, 'raw': b'{}'}
        self.env['ir.attachment'].sudo().create([dict(values) for _i in range(101)])
        target = self.invoice().eta_job_ids
        target.write({'state': 'review', 'historical_review': True, 'historical_fingerprint': 'baseline'})
        target._request_priority_dispatch()
        self.assertEqual(self.Job._priority_dispatch_candidate('all'), target)

    def test_expired_malformed_copied_scope_and_public_requests_are_ignored(self):
        job = self.invoice().eta_job_ids; job.state = 'review'
        job._request_priority_dispatch(); a = self.requests(job)
        original = json.loads(a.raw)
        now = fields.Datetime.now()
        changes = ({'database': 'copied-stage'}, {'company': -1}, {'job': -1},
            {'requested_at': fields.Datetime.to_string(now + timedelta(minutes=1))},
            {'requested_at': fields.Datetime.to_string(now - timedelta(minutes=20)),
             'expires_at': fields.Datetime.to_string(now - timedelta(seconds=1))})
        for change in changes:
            a.raw = json.dumps(dict(original, **change)).encode()
            self.assertFalse(self.Job._priority_dispatch_candidate('all'), change)
        a.raw = b'not json'; self.assertFalse(self.Job._priority_dispatch_candidate('all'))
        a.write({'raw': json.dumps(original).encode(), 'public': True})
        self.assertFalse(self.Job._priority_dispatch_candidate('all'))

    def test_priority_retains_future_backoff_and_only_runs_when_due(self):
        job = self.invoice().eta_job_ids
        job.write({'state': 'review', 'next_attempt': fields.Datetime.now() + timedelta(minutes=50)})
        before = job.next_attempt
        job._request_priority_dispatch()
        self.assertEqual(job.next_attempt, before)
        self.assertFalse(self.Job._priority_dispatch_candidate('all'))
        job.next_attempt = fields.Datetime.now() - timedelta(seconds=1)
        self.assertEqual(self.Job._priority_dispatch_candidate('all'), job)

    def test_callback_exception_cannot_escape_after_invoice_commit(self):
        registry = MagicMock(); model = MagicMock()
        model.sudo.return_value = model; model.browse.return_value = model
        model.exists.return_value = model
        model._dispatch_immediately.side_effect = RuntimeError('private exception contents')
        with patch(MODULE + '.api.Environment', return_value={'tradeline.eta.job': model}), \
                patch(MODULE + '._logger.warning') as log:
            dispatch_after_commit(registry, 7, (22,))
        log.assert_called_once()
        self.assertNotIn('private exception contents', str(log.call_args))
        registry.cursor.assert_called_once()

    def test_dispatch_exception_releases_transport_and_closes_client(self):
        job = self.invoice().eta_job_ids
        def fail(row, client):raise RuntimeError('failure')
        with self.dispatch_harness(processor=fail) as run:
            with self.assertRaises(RuntimeError):job._dispatch_immediately()
        run.client.return_value.close.assert_called_once()
        with self.env.registry.cursor() as other:
            other.execute('SELECT pg_try_advisory_lock(741181,1)')
            self.assertTrue(other.fetchone()[0])
            other.execute('SELECT pg_advisory_unlock(741181,1)')

    def test_sync_requires_invoice_write_access_before_priority_elevation(self):
        move = self.invoice()
        readonly = new_test_user(self.env, login='eta-immediate-readonly', groups='account.group_account_readonly')
        with self.assertRaises(AccessError):move.with_user(readonly).action_send_electronic_invoice()
        self.assertFalse(self.requests(move.eta_job_ids))

    def test_invalid_correction_cannot_clear_uncertain_or_future_retry_marker(self):
        move = self.invoice(); job = move.eta_job_ids
        move.write({'e_invoice_uuid': 'old-invalid', 'e_invoice_status': 'Invalid'})
        job.write({'state': 'unknown', 'attempted_at': fields.Datetime.now(), 'attempts': 1,
            'next_attempt': fields.Datetime.now() + timedelta(minutes=20), 'payload': 'retained request'})
        before = job.read(['state', 'attempted_at', 'attempts', 'payload', 'next_attempt'])
        with self.assertRaisesRegex(UserError, 'reconciliation or a retry deadline'):
            move.action_send_electronic_invoice()
        self.assertEqual(job.read(list(before[0].keys())), before)
        self.assertEqual(move.e_invoice_uuid, 'old-invalid')

    def test_linked_receipt_cannot_replace_a_historical_duplicate_review(self):
        move = self.invoice(); job = move.eta_job_ids
        move.write({'e_invoice_uuid': 'existing-approval', 'e_invoice_status': 'Valid'})
        job.write({'state': 'review', 'historical_review': True, 'historical_fingerprint': 'baseline',
            'last_error': 'Multiple currently Valid ETA receipts need reconciliation'})
        before = job.read(['state', 'historical_review', 'historical_fingerprint', 'last_error'])
        move.action_send_electronic_invoice()
        self.assertEqual(before, job.read(list(before[0].keys())))
        self.assertEqual(len(self.requests(job)), 1)

    def test_queued_transport_retry_keeps_payload_attempt_and_eta_deadline(self):
        move = self.invoice(); job = move.eta_job_ids
        job.write({'state': 'queued', 'attempted_at': fields.Datetime.now(), 'attempts': 1,
            'payload': 'retained throttled request', 'payload_hash': 'same hash',
            'next_attempt': fields.Datetime.now() + timedelta(minutes=2), 'last_error': 'ETA Retry-After'})
        before = job.read(['state', 'attempted_at', 'attempts', 'payload', 'payload_hash', 'next_attempt', 'last_error'])
        move.action_send_electronic_invoice()
        with self.dispatch_harness() as run:self.assertEqual(job._dispatch_immediately(), 0)
        self.assertFalse(run.calls)
        self.assertEqual(before, job.read(list(before[0].keys())))

    def test_manual_priority_survives_later_backoff_without_bypassing_it(self):
        job = self.invoice().eta_job_ids
        now = fields.Datetime.now()
        job.write({'state': 'review', 'historical_fingerprint': 'QA baseline', 'failures': 3})
        with patch(MODULE + '.fields.Datetime.now', return_value=now):
            self.assertTrue(job.with_context(eta_queue_cron=True)._request_priority_dispatch())
        # A later connection failure can move the safe retry beyond the
        # original ten-minute marker; one click must retain its bounded priority.
        job.next_attempt = now + timedelta(minutes=12)
        before = job.read(['state', 'attempts', 'attempted_at', 'payload', 'next_attempt', 'failures'])
        with patch(MODULE + '.fields.Datetime.now', return_value=now + timedelta(minutes=11)):
            self.assertFalse(self.Job._priority_dispatch_candidate('all'))
        with patch(MODULE + '.fields.Datetime.now', return_value=now + timedelta(minutes=13)):
            self.assertEqual(self.Job._priority_dispatch_candidate('all'), job)
        with patch(MODULE + '.fields.Datetime.now', return_value=now + timedelta(hours=2, seconds=1)):
            self.assertFalse(self.Job._priority_dispatch_candidate('all'))
        self.assertEqual(before, job.read(list(before[0].keys())))

    def test_manual_dispatch_precedes_cron_wakeup_and_carries_durable_intent(self):
        move = self.invoice(); callbacks = []
        with patch.object(type(self.env.cr.postcommit), 'add', autospec=True,
                side_effect=lambda owner, callback: callbacks.append(callback)):
            move.action_send_electronic_invoice()
        callback = next(c for c in callbacks if isinstance(c, partial) and c.func is self.callback)
        notify = next(c for c in callbacks if getattr(c, '__name__', '') == '_notifydb'
            and getattr(getattr(c, '__self__', None), 'ids', []) == self.cron.ids)
        self.assertLess(callbacks.index(callback), callbacks.index(notify))
        self.assertEqual(callback.args[3], tuple(self.requests(move.eta_job_ids).ids))

    def test_savepoint_rollback_of_click_cannot_dispatch_an_existing_invoice(self):
        job = self.invoice().eta_job_ids; requests = []
        with self.assertRaisesRegex(UserError, 'undo click'):
            with self.env.cr.savepoint():
                job._request_priority_dispatch(); requests = self.requests(job).ids
                raise UserError('undo click')
        self.assertTrue(job.exists())
        self.assertFalse(self.env['ir.attachment'].sudo().browse(requests).exists())
        registry = MagicMock()
        with patch(MODULE + '.api.Environment', return_value=self.env), \
                patch.object(type(self.Job), '_dispatch_immediately') as dispatch:
            dispatch_after_commit(registry, self.env.uid, tuple(job.ids), tuple(requests))
        dispatch.assert_not_called()

    def test_immediate_dispatch_releases_selected_relation_locks_before_owned_worker(self):
        job = self.invoice().eta_job_ids
        observations = []
        def process(row, client):
            observations.append(self.env.cr.rollback.call_count)
            row.write({'state': 'accepted', 'next_attempt': fields.Datetime.now() + timedelta(minutes=5)})
            return True
        with self.dispatch_harness(processor=process):
            self.assertEqual(job._dispatch_immediately(), 1)
        # One rollback refreshes selection, the other releases its relation
        # locks before entering _process_id on a different connection.
        self.assertEqual(observations, [2])

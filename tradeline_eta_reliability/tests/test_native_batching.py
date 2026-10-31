from contextlib import ExitStack, contextmanager, nullcontext
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from ..models.client import EtaError
from ..models.processing_window import EtaStopped


MODULE = 'odoo.addons.tradeline_eta_reliability.models.eta_job'


class _WorkerEnvironment:
    def __init__(self, cr, job_model, parameters):
        self.cr = cr
        self.models = {'tradeline.eta.job': job_model, 'ir.config_parameter': parameters}

    def __getitem__(self, name):
        return self.models[name]


@tagged('post_install', '-at_install')
class TestNativeBatching(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        values = {'vat': '123456789'}
        for name, value in [('company_size', 'small'), ('company_device', 1)]:
            if name in cls.env['res.partner']._fields:
                values[name] = value
        cls.company.partner_id.with_context(no_vat_validation=True).write(values)
        cls.env.ref('base.EGP').active = True
        cls.company.write({'client_id': 'native-batch-qa', 'client_secret': 'qa-only-secret',
            'config_type': 'uat', 'config_version': '1.0', 'signature_tool': 'python'})
        cls.receivable = cls.env['account.account'].create({'name': 'Native batch receivable', 'code': 'NBATAR',
            'account_type': 'asset_receivable', 'reconcile': True, 'company_ids': [Command.set(cls.company.ids)]})
        cls.income = cls.env['account.account'].create({'name': 'Native batch income', 'code': 'NBATINC',
            'account_type': 'income', 'company_ids': [Command.set(cls.company.ids)]})
        cls.partner = cls.env['res.partner'].create({'name': 'Native batch buyer',
            'property_account_receivable_id': cls.receivable.id})
        cls.journal = cls.env['account.journal'].create({'name': 'Native batch sales', 'code': 'NBAT',
            'type': 'sale', 'company_id': cls.company.id, 'edi_format_ids': [Command.clear()]})

    def setUp(self):
        super().setUp()
        self.Job = self.env['tradeline.eta.job']
        self.cron = self.env.ref('tradeline_eta_reliability.cron_eta_queue')
        self.cron.write({'active': True, 'nextcall': fields.Datetime.now() + timedelta(minutes=1)})
        self.env['ir.config_parameter'].sudo().set_param('tradeline_eta_reliability.enabled_database', self.env.cr.dbname)
        guard = patch('requests.sessions.Session.request', side_effect=AssertionError('Native batch test attempted HTTP'))
        guard.start()
        self.addCleanup(guard.stop)

    def draft(self, **context):
        return self.env['account.move'].with_context(**context).create({
            'move_type': 'out_invoice', 'journal_id': self.journal.id,
            'currency_id': self.env.ref('base.EGP').id, 'partner_id': self.partner.id,
            'invoice_date': fields.Date.today(), 'invoice_line_ids': [Command.create({
                'name': 'Native batch item', 'account_id': self.income.id, 'quantity': 1,
                'price_unit': 100, 'tax_ids': [Command.clear()]})]})

    def trigger_count(self):
        return self.env['ir.cron.trigger'].search_count([('cron_id', '=', self.cron.id)])

    def fake_jobs(self, count, state='queued'):
        now = fields.Datetime.now()
        return [SimpleNamespace(id=i + 1, state=state, next_attempt=False, company_id=self.company,
            historical_fingerprint=False if state == 'queued' else 'baseline', historical_reviewed_at=False,
            checked_at=now, create_date=now - timedelta(seconds=count - i)) for i in range(count)]

    @contextmanager
    def native_harness(self, jobs, processor=None, seconds=0, real_progress=False):
        clock = [0.0]
        calls = []

        def due(kind):
            states = ['queued', 'unknown'] if kind == 'submit' else ['accepted'] if kind == 'status' else ['queued', 'accepted', 'unknown', 'review']
            return [job for job in jobs if job.state in states
                    and (not job.next_attempt or job.next_attempt <= fields.Datetime.now())]

        def process(job_id, client):
            calls.append(job_id)
            clock[0] += seconds
            job = next(job for job in jobs if job.id == job_id)
            if processor:
                return processor(job)
            job.state = 'valid'
            return True

        def count(domain, **kwargs):
            states = domain[0][2]
            return len([job for job in jobs if job.state in states
                        and (not job.next_attempt or job.next_attempt <= fields.Datetime.now())])

        with ExitStack() as stack:
            stack.enter_context(patch.object(self.env.cr, 'commit'))
            stack.enter_context(patch.object(self.env.cr, 'rollback'))
            stack.enter_context(patch(MODULE + '.check_processing_window'))
            stack.enter_context(patch(MODULE + '.time.monotonic', side_effect=lambda: clock[0]))
            clients = stack.enter_context(patch(MODULE + '.EtaClient'))
            stack.enter_context(patch.object(type(self.Job), '_native_maintenance', return_value=False))
            stack.enter_context(patch.object(type(self.Job), '_due_jobs', side_effect=due))
            stack.enter_context(patch.object(type(self.Job), '_process_id', side_effect=process))
            stack.enter_context(patch.object(type(self.Job), 'search_count', side_effect=count))
            notify = None if real_progress else stack.enter_context(patch.object(type(self.cron), '_notify_progress'))
            yield SimpleNamespace(clock=clock, calls=calls, clients=clients, notify=notify)

    @contextmanager
    def isolated_worker(self, *, state='queued', future=False, binding=True, locked=True):
        cr = MagicMock()
        cr.dbname = self.env.cr.dbname
        cr.__enter__.return_value = cr
        cr.__exit__.return_value = False
        cr.fetchone.side_effect = [(cr.dbname if binding else 'another-database',), (locked,)]
        move = SimpleNamespace(id=991, state='posted', invalidate_recordset=MagicMock())
        job = SimpleNamespace(state=state, next_attempt=fields.Datetime.now() + timedelta(minutes=5) if future else False,
            company_id=SimpleNamespace(id=1), move_id=move, invalidate_recordset=MagicMock(),
            _submit=MagicMock(), _refresh=MagicMock(), _delay=MagicMock(), write=MagicMock())
        job.exists = lambda: job
        job.with_company = lambda company: job
        model = MagicMock()
        model.sudo.return_value = model
        model.browse.return_value = job
        params = MagicMock()
        params.sudo.return_value = params
        params.get_param.return_value = cr.dbname
        env = _WorkerEnvironment(cr, model, params)
        with patch.object(self.env.registry, 'cursor', return_value=cr), \
                patch(MODULE + '.api.Environment', return_value=env), \
                patch(MODULE + '.check_processing_window') as window:
            yield SimpleNamespace(cr=cr, job=job, params=params, window=window, client=MagicMock())

    def test_posted_recordset_wakes_native_scheduler_once_without_http(self):
        moves = self.draft() | self.draft()
        before = self.trigger_count()
        moves.action_post()
        self.assertEqual(self.trigger_count() - before, 1)
        self.assertEqual(len(moves.eta_job_ids), 2)
        self.assertEqual(set(moves.eta_job_ids.mapped('state')), {'queued'})
        moves._eta_enqueue()
        self.assertEqual(self.trigger_count() - before, 1)

    def test_posting_rollback_removes_queue_record_and_wakeup_trigger(self):
        jobs = triggers = []
        with self.assertRaisesRegex(UserError, 'posting rollback'):
            with self.env.cr.savepoint():
                before = self.env['ir.cron.trigger'].search([('cron_id', '=', self.cron.id)])
                move = self.draft()
                move.action_post()
                jobs = move.eta_job_ids.ids
                triggers = (self.env['ir.cron.trigger'].search([('cron_id', '=', self.cron.id)]) - before).ids
                self.assertEqual(len(triggers), 1)
                raise UserError('posting rollback')
        self.assertFalse(self.Job.browse(jobs).exists())
        self.assertFalse(self.env['ir.cron.trigger'].browse(triggers).exists())

    def test_enqueue_does_not_self_trigger_from_current_eta_cron(self):
        before = self.trigger_count()
        move = self.draft(cron_id=self.cron.id)
        move.action_post()
        self.assertTrue(move.eta_job_ids)
        self.assertEqual(self.trigger_count(), before)
        move = self.draft(eta_queue_cron=True)
        move.action_post()
        self.assertTrue(move.eta_job_ids)
        self.assertEqual(self.trigger_count(), before)

    def test_copy_database_disabled_cron_and_pause_never_wake(self):
        before = self.trigger_count()
        self.env['ir.config_parameter'].sudo().set_param('tradeline_eta_reliability.enabled_database', 'copied-database')
        self.draft().action_post()
        self.assertEqual(self.trigger_count(), before)
        self.env['ir.config_parameter'].sudo().set_param('tradeline_eta_reliability.enabled_database', self.env.cr.dbname)
        self.cron.active = False
        self.draft().action_post()
        self.assertEqual(self.trigger_count(), before)
        self.cron.active = True
        with patch('odoo.addons.tradeline_eta_reliability.models.account_move.check_processing_window', side_effect=EtaStopped('Owner paused')):
            self.draft().action_post()
        self.assertEqual(self.trigger_count(), before)

    def test_accepted_or_uncertain_new_records_do_not_get_fresh_queue_wakeup(self):
        before = self.trigger_count()
        move = self.draft()
        move.e_invoice_uuid = 'already-received'
        move.action_post()
        self.assertEqual(move.eta_job_ids.state, 'accepted')
        move = self.draft()
        move.e_invoice_sent = True
        move.action_post()
        self.assertEqual(move.eta_job_ids.state, 'unknown')
        self.assertEqual(self.trigger_count(), before)

    def test_native_operation_cap_and_truthful_remaining_progress(self):
        jobs = self.fake_jobs(6)
        with self.native_harness(jobs) as run:
            count = self.Job.with_context(ir_cron_progress_id=123)._cron_process()
        self.assertEqual((count, len(run.calls)), (4, 4))
        run.notify.assert_called_once_with(done=4, remaining=2)
        run.clients.return_value.close.assert_called_once()

    def test_native_soft_time_budget_stops_starting_more_work(self):
        jobs = self.fake_jobs(6)
        with self.native_harness(jobs, seconds=3) as run:
            count = self.Job.with_context(ir_cron_progress_id=123)._cron_process()
        self.assertEqual((count, len(run.calls), run.clock[0]), (3, 3, 9))
        run.notify.assert_called_once_with(done=3, remaining=3)

    def test_skipped_job_lock_does_not_claim_progress_or_spin(self):
        jobs = self.fake_jobs(1)
        with self.native_harness(jobs, processor=lambda job: False) as run:
            count = self.Job.with_context(ir_cron_progress_id=123)._cron_process()
        self.assertEqual(count, 0)
        self.assertEqual(len(run.calls), 4)
        run.notify.assert_not_called()

    def test_remaining_excludes_future_retry_dates_and_permanent_holds(self):
        jobs = self.fake_jobs(1)
        future = self.fake_jobs(1, 'accepted')[0]
        future.id = 9
        future.next_attempt = fields.Datetime.now() + timedelta(minutes=20)
        held = self.fake_jobs(1, 'blocked')[0]
        held.id = 10
        jobs += [future, held]
        with self.native_harness(jobs) as run:
            self.Job.with_context(ir_cron_progress_id=123)._cron_process()
        run.notify.assert_called_once_with(done=1, remaining=0)
        self.assertEqual(run.calls, [1])

    def test_real_due_domain_respects_kind_retry_deadlines_and_terminal_states(self):
        moves = self.draft()
        for _index in range(5):
            moves |= self.draft()
        moves.action_post()
        jobs = moves.eta_job_ids.sorted('id')
        for job, state in zip(jobs, ('queued', 'accepted', 'unknown', 'review', 'blocked', 'valid')):
            job.state = state
        jobs[1].next_attempt = fields.Datetime.now() + timedelta(minutes=10)
        self.assertEqual(self.Job.search(self.Job._native_due_domain()) & jobs, jobs[0] | jobs[2] | jobs[3])
        self.assertEqual(self.Job.search(self.Job._native_due_domain('submit')) & jobs, jobs[0] | jobs[2])
        self.assertFalse(self.Job.search(self.Job._native_due_domain('status')) & jobs)
        jobs[1].next_attempt = False
        self.assertEqual(self.Job.search(self.Job._native_due_domain('status')) & jobs, jobs[1])

    def test_empty_due_queue_does_not_claim_progress(self):
        with self.native_harness([]) as run:
            self.assertEqual(self.Job.with_context(ir_cron_progress_id=123)._cron_process(), 0)
        self.assertFalse(run.calls)
        run.notify.assert_not_called()

    def test_native_selection_prioritizes_fresh_uncertain_stale_then_verified(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'tradeline_eta_reliability.native_review_next_class', 'null')
        review, accepted, verified, unknown, fresh = [self.fake_jobs(1, state)[0]
            for state in ('review', 'accepted', 'queued', 'unknown', 'queued')]
        for index, job in enumerate((review, accepted, verified, unknown, fresh), 1):
            job.id = index
        verified.historical_fingerprint = 'baseline'
        verified.historical_reviewed_at = fields.Datetime.now()
        accepted.checked_at = fields.Datetime.now() - timedelta(minutes=6)
        jobs = [review, accepted, verified, unknown, fresh]
        with patch.object(type(self.Job), '_due_jobs', side_effect=lambda kind: jobs):
            self.assertIs(self.Job._native_batch_candidate('all', 0), fresh)
            jobs.remove(fresh)
            self.assertIs(self.Job._native_batch_candidate('all', 0), unknown)
            jobs.remove(unknown)
            self.assertIs(self.Job._native_batch_candidate('all', 0), accepted)
            jobs.remove(accepted)
            self.assertIs(self.Job._native_batch_candidate('all', 0), verified)

    def test_review_page_includes_overdue_jobs_beyond_large_null_backlog(self):
        moves = self.draft()
        for _index in range(44):
            moves |= self.draft()
        moves.action_post()
        jobs = moves.eta_job_ids.sorted('id')
        jobs.write({'state': 'review', 'next_attempt': False})
        jobs[-2].next_attempt = fields.Datetime.now() - timedelta(days=2)
        jobs[-1].next_attempt = fields.Datetime.now() + timedelta(minutes=10)
        due = self.Job._due_jobs('all')
        self.assertIn(jobs[-2], due)
        self.assertNotIn(jobs[-1], due)
        self.assertEqual(len([job for job in due if job.id in jobs[:-2].ids]), 20)

    def test_review_class_alternates_across_single_operation_callbacks(self):
        dated, untouched = self.fake_jobs(2, 'review')
        dated.next_attempt = fields.Datetime.now() - timedelta(days=2)
        parameters = self.env['ir.config_parameter'].sudo()
        parameters.set_param('tradeline_eta_reliability.native_review_next_class', 'dated')
        with self.native_harness([dated, untouched], seconds=9) as run:
            self.assertEqual(self.Job._cron_process(), 1)
            self.assertEqual(run.calls, [dated.id])
        self.assertEqual(parameters.get_param('tradeline_eta_reliability.native_review_next_class'), 'null')
        with self.native_harness([dated, untouched], seconds=9) as run:
            self.assertEqual(self.Job._cron_process(), 1)
            self.assertEqual(run.calls, [untouched.id])
        self.assertEqual(parameters.get_param('tradeline_eta_reliability.native_review_next_class'), 'dated')

    def test_missing_preferred_review_class_still_progresses(self):
        untouched = self.fake_jobs(1, 'review')[0]
        self.env['ir.config_parameter'].sudo().set_param(
            'tradeline_eta_reliability.native_review_next_class', 'dated')
        with self.native_harness([untouched]) as run:
            self.assertEqual(self.Job._cron_process(), 1)
        self.assertEqual(run.calls, [untouched.id])

    def test_busy_review_job_does_not_advance_persistent_class(self):
        dated = self.fake_jobs(1, 'review')[0]
        dated.next_attempt = fields.Datetime.now() - timedelta(days=2)
        parameters = self.env['ir.config_parameter'].sudo()
        parameters.set_param('tradeline_eta_reliability.native_review_next_class', 'dated')
        with self.native_harness([dated], processor=lambda job: False) as run:
            self.assertEqual(self.Job._cron_process(), 0)
        self.assertEqual(len(run.calls), 4)
        self.assertEqual(parameters.get_param('tradeline_eta_reliability.native_review_next_class'), 'dated')

    def test_real_orm_cache_is_refreshed_after_an_isolated_processing_result(self):
        move = self.draft()
        move.action_post()
        job = move.eta_job_ids
        self.env.flush_all()
        self.assertEqual(job.state, 'queued')

        def processed(record):
            self.env.cr.execute("UPDATE tradeline_eta_job SET state='valid' WHERE id=%s", [record.id])
            return True

        with self.native_harness([job], processor=processed) as run:
            self.Job.with_context(ir_cron_progress_id=123)._cron_process()
        self.assertEqual(run.calls, [job.id])
        run.notify.assert_called_once_with(done=1, remaining=0)

    def test_global_lock_busy_copy_database_and_paused_have_no_progress(self):
        jobs = self.fake_jobs(1)
        self.env['ir.config_parameter'].sudo().set_param('tradeline_eta_reliability.enabled_database', 'other-database')
        self.env.flush_all()
        with self.native_harness(jobs) as run:
            self.assertEqual(self.Job.with_context(ir_cron_progress_id=123)._cron_process(), 0)
        self.assertFalse(run.calls)
        run.notify.assert_not_called()
        self.env['ir.config_parameter'].sudo().set_param('tradeline_eta_reliability.enabled_database', self.env.cr.dbname)
        self.env.flush_all()
        with self.native_harness(jobs) as run, patch(MODULE + '.check_processing_window', side_effect=EtaStopped('Owner paused')):
            self.assertEqual(self.Job.with_context(ir_cron_progress_id=123)._cron_process(), 0)
        self.assertFalse(run.calls)
        run.notify.assert_not_called()
        execute = self.env.cr.execute

        def busy(query, *args, **kwargs):
            if 'pg_try_advisory_lock(741181' in str(query):
                return None
            return execute(query, *args, **kwargs)

        # Do not acquire a real session lock and then lie about its result:
        # that would leak a reentrant advisory lock into later QA tests.
        with self.native_harness(jobs) as run, \
                patch.object(self.env.cr, 'execute', side_effect=busy), \
                patch.object(self.env.cr, 'fetchone', side_effect=[(self.env.cr.dbname,), (False,)]):
            self.assertEqual(self.Job.with_context(ir_cron_progress_id=123)._cron_process(), 0)
        self.assertFalse(run.calls)
        run.notify.assert_not_called()

    def test_pause_after_a_committed_operation_does_not_schedule_more_batches(self):
        jobs = self.fake_jobs(2)
        with self.native_harness(jobs) as run, patch(MODULE + '.check_processing_window', side_effect=[None, None, None, EtaStopped('Owner paused')]):
            self.Job.with_context(ir_cron_progress_id=123)._cron_process()
        self.assertEqual(len(run.calls), 1)
        run.notify.assert_not_called()

    def test_maintenance_cadence_is_database_bound_and_not_per_core_callback(self):
        self.env['ir.config_parameter'].sudo().set_param('tradeline_eta_reliability.historical_recovery_database', self.env.cr.dbname)
        self.env['ir.config_parameter'].sudo().set_param('tradeline_eta_reliability.native_maintenance_at', '')
        now = fields.Datetime.now()
        with patch.object(type(self.Job), '_enroll_historical') as enroll, \
                patch.object(type(self.Job), '_requeue_software_holds') as software, \
                patch.object(type(self.Job), '_requeue_changed_historical') as changed, \
                patch.object(type(self.env['account.move']), 'search', return_value=self.env['account.move']), \
                patch(MODULE + '.fields.Datetime.now', return_value=now):
            jobs = self.Job.with_context(eta_queue_cron=True)
            self.assertTrue(jobs._native_maintenance())
            self.env.flush_all()
            self.assertFalse(jobs._native_maintenance())
            with patch(MODULE + '.fields.Datetime.now', return_value=now + timedelta(seconds=121)):
                self.assertTrue(jobs._native_maintenance())
            self.assertEqual(enroll.call_count, 2)
            self.assertEqual(software.call_count, 2)
            self.assertEqual(changed.call_count, 2)

    def test_isolated_processor_returns_true_for_committed_work(self):
        with self.isolated_worker() as run:
            self.assertTrue(self.Job._process_id(1, run.client))
        run.job._submit.assert_called_once_with(run.client)
        run.params.set_param.assert_called_once()
        self.assertTrue(any('pg_advisory_unlock' in str(call) for call in run.cr.execute.call_args_list))

    def test_isolated_processor_returns_false_for_stage_pause_skip_and_stale_due(self):
        for options in ({'binding': False}, {'locked': False}, {'state': 'valid'}, {'future': True}):
            with self.isolated_worker(**options) as run:
                self.assertFalse(self.Job._process_id(1, run.client))
            run.job._submit.assert_not_called()
            run.job._refresh.assert_not_called()
            run.params.set_param.assert_not_called()
        with self.isolated_worker() as run:
            run.window.side_effect = EtaStopped('Owner paused')
            self.assertFalse(self.Job._process_id(1, run.client))
        run.job._submit.assert_not_called()

    def test_isolated_processor_committed_technical_deferral_is_real_progress(self):
        with self.isolated_worker() as run:
            run.job._submit.side_effect = EtaError('Temporary ETA failure', retry_after=120)
            self.assertTrue(self.Job._process_id(1, run.client))
        run.job._delay.assert_called_once()

    def test_pause_keeps_a_durable_uncertain_marker_and_reports_no_new_result(self):
        with self.isolated_worker() as run:
            run.job.payload = 'retained signed request'

            def durable_marker(client):
                run.job.state = 'unknown'
                run.cr.commit()

            run.job._submit.side_effect = durable_marker
            run.window.side_effect = [None, EtaStopped('Owner paused')]
            self.assertFalse(self.Job._process_id(1, run.client))
        self.assertEqual((run.job.state, run.job.payload), ('unknown', 'retained signed request'))
        run.job._delay.assert_not_called()
        run.client.request.assert_not_called()

    def _core_run(self, jobs):
        callbacks = []

        def callback(cron, name, action_id):
            callbacks.append(cron.env.context.get('ir_cron_progress_id'))
            cron.env['tradeline.eta.job']._cron_process()

        job = {'id': self.cron.id, 'user_id': self.env.uid, 'lastcall': False,
            'timed_out_counter': 0, 'cron_name': 'Native batching QA',
            'ir_actions_server_id': self.cron.ir_actions_server_id.id}
        with self.native_harness(jobs, real_progress=True) as run, \
                patch.object(self.env.registry, 'cursor', side_effect=lambda *args, **kwargs: nullcontext(self.env.cr)), \
                patch.object(type(self.cron), '_callback', autospec=True, side_effect=callback):
            status = self.cron._run_job(job)
        return status, callbacks, run

    def test_actual_core_repeats_bounded_batches_until_current_due_work_is_done(self):
        status, callbacks, run = self._core_run(self.fake_jobs(5))
        self.assertEqual(status, 'fully done')
        self.assertEqual(len(callbacks), 2)
        self.assertTrue(all(callbacks))
        self.assertEqual(len(run.calls), 5)

    def test_actual_core_yields_after_ten_batches_with_truthful_work_remaining(self):
        jobs = self.fake_jobs(45)
        status, callbacks, run = self._core_run(jobs)
        self.assertEqual(status, 'partially done')
        self.assertEqual(len(callbacks), 10)
        self.assertEqual(len(run.calls), 40)
        self.assertEqual(sum(job.state == 'queued' for job in jobs), 5)

    def _partial_progress(self, now, *, age=30, done=4, remaining=20, timed_out=0):
        progress = self.env['ir.cron.progress'].create({'cron_id': self.cron.id,
            'done': done, 'remaining': remaining, 'timed_out_counter': timed_out})
        self.env.flush_all()
        self.env.cr.execute('UPDATE ir_cron_progress SET write_date=%s WHERE id=%s',
                            [now - timedelta(seconds=age), progress.id])
        progress.invalidate_recordset()
        return progress

    def _eta_observations(self, job, now):
        monitor = self.env['tradeline.operations.monitor'].with_context(active_test=False).search([], limit=1)
        monitor = monitor or self.env['tradeline.operations.monitor'].create({})
        with patch.object(type(self.Job), 'search_count', side_effect=lambda domain, **kw: len(job.filtered_domain(domain))):
            return {item['code'] for item in monitor._probe_crons(now) if item['code'].startswith('eta:')}

    def test_recent_committed_partial_batch_is_healthy_with_past_nextcall(self):
        move = self.draft()
        move.action_post()
        job = move.eta_job_ids
        now = fields.Datetime.now()
        self.cron.nextcall = now - timedelta(hours=1)
        self.env['ir.config_parameter'].sudo().set_param(
            'tradeline_eta_reliability.last_processed_at', fields.Datetime.to_string(now - timedelta(seconds=15)))
        self._partial_progress(now)
        self.assertTrue(self.Job._native_partial_progress_is_recent(now, self.cron))
        self.assertNotIn('eta:scheduler', self._eta_observations(job, now))
        self.assertNotIn('eta:progress', self._eta_observations(job, now))
        self.assertNotIn('eta:stalled', self._eta_observations(job, now))
        job._compute_monitoring()
        self.assertNotIn('overdue', job.reason_summary.lower())
        self.assertIn('Waiting in the automatic submission queue', job.reason_summary)

    def test_partial_progress_does_not_hide_stale_invoice_processing(self):
        move = self.draft()
        move.action_post()
        job = move.eta_job_ids
        now = fields.Datetime.now()
        self.cron.nextcall = now - timedelta(hours=1)
        self.env['ir.config_parameter'].sudo().set_param(
            'tradeline_eta_reliability.last_processed_at', fields.Datetime.to_string(now - timedelta(minutes=16)))
        self._partial_progress(now)
        self.assertFalse(self.Job._native_partial_progress_is_recent(now, self.cron))
        observations = self._eta_observations(job, now)
        self.assertIn('eta:scheduler', observations)
        self.assertIn('eta:progress', observations)
        job._compute_monitoring()
        self.assertIn('overdue', job.reason_summary.lower())

    def test_fresh_processing_alone_and_old_or_incomplete_core_rows_are_not_partial_health(self):
        now = fields.Datetime.now()
        self.env['ir.config_parameter'].sudo().set_param(
            'tradeline_eta_reliability.last_processed_at', fields.Datetime.to_string(now))
        for values in ({'age': 181}, {'done': 0}, {'remaining': 0}, {'timed_out': 1}):
            progress = self._partial_progress(now, **values)
            self.assertFalse(self.Job._native_partial_progress_is_recent(now, self.cron))
            progress.unlink()
        self.assertFalse(self.Job._native_partial_progress_is_recent(now, self.cron))

    def test_partial_health_cannot_override_disabled_failed_or_wrong_database_scheduler(self):
        now = fields.Datetime.now()
        self.env['ir.config_parameter'].sudo().set_param(
            'tradeline_eta_reliability.last_processed_at', fields.Datetime.to_string(now))
        self._partial_progress(now)
        move = self.draft()
        move.action_post()
        job = move.eta_job_ids
        self.cron.active = False
        self.assertFalse(self.Job._native_partial_progress_is_recent(now, self.cron))
        self.assertIn('eta:scheduler', self._eta_observations(job, now))
        self.cron.write({'active': True, 'failure_count': 1})
        self.assertFalse(self.Job._native_partial_progress_is_recent(now, self.cron))
        self.assertIn('eta:scheduler', self._eta_observations(job, now))
        self.cron.failure_count = 0
        self.env['ir.config_parameter'].sudo().set_param('tradeline_eta_reliability.enabled_database', 'copied-database')
        self.assertFalse(self.Job._native_partial_progress_is_recent(now, self.cron))
        self.assertIn('eta:scheduler', self._eta_observations(job, now))

    def test_healthy_partial_scheduler_does_not_hide_genuine_uncertain_outcome(self):
        move = self.draft()
        move.action_post()
        job = move.eta_job_ids
        job.write({'state': 'unknown', 'last_error': 'A submitted request lost its response.'})
        now = fields.Datetime.now()
        self.cron.nextcall = now - timedelta(hours=1)
        self.env['ir.config_parameter'].sudo().set_param(
            'tradeline_eta_reliability.last_processed_at', fields.Datetime.to_string(now))
        self._partial_progress(now)
        observations = self._eta_observations(job, now)
        self.assertNotIn('eta:scheduler', observations)
        self.assertIn('eta:stalled', observations)
        job._compute_monitoring()
        self.assertTrue(job.needs_attention)
        self.assertIn('reconcile the existing eta outcome first', job.required_action.lower())
        self.assertIn('do not blindly resend', job.required_action.lower())

    def test_malformed_or_future_processing_markers_cannot_claim_partial_health(self):
        now = fields.Datetime.now()
        self._partial_progress(now)
        for marker in ('not a timestamp', fields.Datetime.to_string(now + timedelta(minutes=1)), False):
            self.env['ir.config_parameter'].sudo().set_param('tradeline_eta_reliability.last_processed_at', marker)
            self.assertFalse(self.Job._native_partial_progress_is_recent(now, self.cron))

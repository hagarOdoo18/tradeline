import json
import hashlib
from contextlib import ExitStack, contextmanager, nullcontext
from datetime import timedelta
from unittest.mock import MagicMock, patch

from lxml import etree
from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user

from ..models.client import EtaError
from ..models.eta_job import STAFF_SEND, STAFF_STATUS
from ..models.staff_workflow import REQUEST_NAME, StatusClient

MODULE = 'odoo.addons.tradeline_eta_reliability.models.staff_workflow'


@tagged('post_install', '-at_install')
class TestEtaStaffWorkflow(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        values = {'vat': '123456789'}
        for name, value in [('company_size', 'small'), ('company_device', 1)]:
            if name in cls.company.partner_id._fields:
                values[name] = value
        cls.company.partner_id.with_context(no_vat_validation=True).write(values)
        cls.company.write({'client_id': 'staff-qa', 'client_secret': 'qa-only-secret', 'config_type': 'uat',
                           'config_version': '1.0', 'signature_tool': 'python'})
        cls.env.ref('base.EGP').active = True
        cls.receivable = cls.env['account.account'].create({'name': 'Staff QA receivable', 'code': 'STFAR',
            'account_type': 'asset_receivable', 'reconcile': True, 'company_ids': [Command.set(cls.company.ids)]})
        cls.income = cls.env['account.account'].create({'name': 'Staff QA income', 'code': 'STFINC',
            'account_type': 'income', 'company_ids': [Command.set(cls.company.ids)]})
        cls.partner = cls.env['res.partner'].create({'name': 'Staff QA buyer',
            'property_account_receivable_id': cls.receivable.id})
        cls.journal = cls.env['account.journal'].create({'name': 'Staff QA sales', 'code': 'STF',
            'type': 'sale', 'company_id': cls.company.id, 'edi_format_ids': [Command.clear()]})
        cls.clerk = new_test_user(cls.env, login='eta_staff_workflow_clerk', groups='account.group_account_invoice,tradeline_eta_reliability.group_eta_workspace',
            company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)])

    def setUp(self):
        super().setUp()
        self.Job = self.env['tradeline.eta.job']
        self.env['ir.config_parameter'].sudo().set_param('tradeline_eta_reliability.enabled_database', self.env.cr.dbname)
        guard = patch('requests.sessions.Session.request', side_effect=AssertionError('Staff test attempted real HTTP'))
        guard.start(); self.addCleanup(guard.stop)

    def invoice(self):
        move = self.env['account.move'].with_context(eta_queue_cron=True).create({
            'move_type': 'out_invoice', 'journal_id': self.journal.id, 'partner_id': self.partner.id,
            'currency_id': self.env.ref('base.EGP').id, 'invoice_date': fields.Date.today(),
            'invoice_line_ids': [Command.create({'name': 'Staff QA item', 'account_id': self.income.id,
                'quantity': 1, 'price_unit': 100, 'tax_ids': [Command.clear()]})]})
        move.action_post()
        return move

    @contextmanager
    def owned_worker(self, client=None):
        """Exercise the real worker on rollback-only fixtures, with fake transport."""
        client = client or MagicMock(company=self.company)
        client.staff_error = False
        self.env.flush_all()
        with ExitStack() as stack:
            stack.enter_context(patch.object(self.env.registry, 'cursor', side_effect=lambda: nullcontext(self.env.cr)))
            stack.enter_context(patch.object(self.env.cr, 'commit', side_effect=self.env.flush_all))
            stack.enter_context(patch.object(self.env.cr, 'rollback', side_effect=self.env.flush_all))
            stack.enter_context(patch(MODULE + '.EtaClient', return_value=client))
            yield client

    def accept(self, job, client):
        def prepare(row):
            row.write({'payload': json.dumps({'documents': [{'internalID': row.move_id.name,
                'dateTimeIssued': row.move_id._eta_issue_timestamp(), 'invoiceLines': [{'quantity': 1}],
                'signatures': [{'value': 'qa-only'}]}]}), 'payload_hash': 'qa'})
        def request(method, path, **kwargs):
            self.assertEqual(method, 'POST')
            self.assertEqual(path, '/api/v1/documentsubmissions')
            self.assertEqual(job.state, 'unknown', 'The uncertainty marker must precede POST')
            self.assertEqual(job.attempts, 1)
            return {'acceptedDocuments': [{'internalId': job.move_id.name, 'uuid': 'staff-qa-receipt'}],
                    'submissionUUID': 'staff-qa-submission'}
        client.request.side_effect = request
        return (patch.object(type(job), '_prepare', autospec=True, side_effect=prepare),
                patch.object(type(job), '_preflight_submission', return_value=True))

    def test_intake_rejection_groups_with_eta_rejections_without_changing_transport(self):
        move = self.invoice()
        job = move.eta_job_ids
        reason = 'ETA rejected the submission (1). {"code":"1","details":[{"code":"CF305"}]}'
        job.write({'state': 'blocked', 'safe_to_retry': True, 'attempts': 1,
                   'attempted_at': fields.Datetime.now(), 'last_error': reason,
                   'payload': 'retained-request', 'payload_hash': 'retained-hash'})
        move.write({'e_invoice_status': 'Error', 'e_invoice_issue': reason})
        self.assertEqual(job.staff_state, 'invalid')
        self.assertEqual(move.eta_staff_state, 'invalid')
        keep = ['state', 'payload', 'payload_hash', 'attempts', 'attempted_at',
                'safe_to_retry', 'next_attempt', 'write_date']
        self.env.flush_all()
        before = job.read(keep)
        self.env.cr.execute("UPDATE tradeline_eta_job SET staff_state='blocked' WHERE id=%s", [job.id])
        job.invalidate_recordset(['staff_state'])
        self.assertEqual(job.staff_state, 'blocked')
        self.assertGreaterEqual(self.Job._refresh_blocked_staff_rejection_labels(), 1)
        self.assertEqual(job.staff_state, 'invalid')
        self.assertEqual(before, job.read(keep))
        self.assertIn(job.id, self.Job.search([('staff_state', '=', 'invalid')]).ids)
        self.assertEqual(move.e_invoice_status, 'Error')
        with self.assertRaises(AccessError):
            self.Job.with_user(self.clerk)._refresh_blocked_staff_rejection_labels()

    def test_attempt_or_error_alone_never_claims_an_eta_rejection(self):
        move = self.invoice()
        job = move.eta_job_ids
        reason = 'ETA rejected the submission (1). {"code":"1"}'
        job.write({'state': 'blocked', 'safe_to_retry': True, 'attempts': 1,
                   'attempted_at': fields.Datetime.now(), 'last_error': reason})
        move.write({'e_invoice_status': 'Error', 'e_invoice_issue': reason})
        self.assertEqual(job.staff_state, 'invalid')
        for values in ({'last_error': 'Receiver Id is required'}, {'safe_to_retry': False},
                       {'attempts': 0}, {'attempted_at': False}):
            old = {name: job[name] for name in values}
            job.write(values)
            self.assertEqual(job.staff_state, 'blocked')
            job.write(old)
        move.e_invoice_issue = 'Different or stale rejection'
        self.assertEqual(job.staff_state, 'blocked')
        move.e_invoice_issue = reason
        job.state = 'unknown'
        self.assertEqual(job.staff_state, 'unknown')

    def test_current_invalid_receipt_does_not_disappear_into_generic_blocked_group(self):
        move = self.invoice()
        job = move.eta_job_ids
        job.write({'state': 'blocked', 'last_error': 'Reconcile duplicate receipt before retrying.'})
        move.write({'e_invoice_status': 'Invalid', 'e_invoice_uuid': 'current-invalid-receipt'})
        self.assertEqual(job.staff_state, 'invalid')
        self.assertEqual(job.state, 'blocked')
        move.l10n_eg_uuid = 'conflicting-other-receipt'
        self.assertEqual(job.staff_state, 'blocked')
        move.l10n_eg_uuid = False
        move.e_invoice_status = 'Valid'
        self.assertEqual(job.staff_state, 'blocked')

    def test_normal_invoice_user_requests_direct_operation_without_queue_wakeup_or_http(self):
        move = self.invoice().with_user(self.clerk)
        with patch.object(type(self.Job), '_request_priority_dispatch', side_effect=AssertionError('Queued manual send')), \
                patch.object(type(self.Job), '_schedule_immediate_dispatch', side_effect=AssertionError('Postcommit queue dispatch')):
            action = move.action_eta_send_now()
        self.assertEqual(action['tag'], 'tradeline_eta_staff_operation')
        attachment = self.env['ir.attachment'].browse(action['params']['request_id'])
        data = json.loads(attachment.raw)
        self.assertEqual((data['uid'], data['mode'], data['invoice']), (self.clerk.id, 'send', move.id))
        self.assertEqual(attachment.name, REQUEST_NAME); self.assertFalse(attachment.public)
        self.assertEqual(move.eta_job_ids.attempts, 0)

    def test_direct_send_posts_while_backlog_global_lock_is_owned_and_cron_disabled(self):
        move = self.invoice(); job = move.eta_job_ids
        self.env.ref('tradeline_eta_reliability.cron_eta_queue').active = False
        unrelated = self.invoice().eta_job_ids
        with self.env.registry.cursor() as other:
            other.execute('SELECT pg_advisory_lock(741181,1)')
            try:
                client = MagicMock(company=self.company)
                prepare, preflight = self.accept(job, client)
                with self.owned_worker(client), prepare, preflight:
                    done, message = job._staff_dispatch('send')
                self.assertTrue(done); self.assertFalse(message)
            finally:
                other.execute('SELECT pg_advisory_unlock(741181,1)')
        self.assertEqual(job.state, 'accepted'); self.assertEqual(job.attempts, 1)
        self.assertEqual(unrelated.state, 'queued'); self.assertFalse(unrelated.attempts)
        self.assertEqual(move.amount_total, 100)

    def test_same_invoice_worker_lock_prevents_a_second_manual_post(self):
        job = self.invoice().eta_job_ids
        with self.env.registry.cursor() as other:
            other.execute('SELECT pg_advisory_lock(741180,%s)', [job.id])
            try:
                with self.owned_worker() as client:
                    done, message = job._staff_dispatch('send')
                self.assertFalse(done); self.assertTrue(message); client.request.assert_not_called()
            finally:
                other.execute('SELECT pg_advisory_unlock(741180,%s)', [job.id])

    def test_invoice_user_executes_saved_intent_and_replay_cannot_submit_again(self):
        self.clerk.partner_id.email = False
        move = self.invoice().with_user(self.clerk); job = move.eta_job_ids.sudo()
        action = move.action_eta_send_now()
        client = MagicMock(company=self.company)
        prepare, preflight = self.accept(job, client)
        with self.owned_worker(client), prepare, preflight:
            result = move.action_eta_execute_staff_request(action['params']['request_id'])
            replay = move.action_eta_execute_staff_request(action['params']['request_id'])
        self.assertIn('Received by ETA', result['title'])
        self.assertIn('already started', replay['title'])
        self.assertEqual(client.request.call_count, 1); self.assertEqual(job.attempts, 1)
        audit = json.loads(self.env['ir.attachment'].browse(action['params']['request_id']).raw)
        self.assertEqual(audit['state'], 'finished'); self.assertEqual(audit['uid'], self.clerk.id)

    def test_clerk_sync_resends_a_corrected_terminal_rejection_once(self):
        move = self.invoice().with_user(self.clerk); job = move.eta_job_ids.sudo()
        original = {'internalID': move.name, 'issuer': {'id': self.company.vat},
            'receiver': {'id': 'old-id'}, 'totalAmount': 100,
            'dateTimeIssued': move._eta_issue_timestamp(),
            'invoiceLines': [{'quantity': 1}], 'signatures': [{'value': 'old-signature'}]}
        payload = json.dumps({'documents': [original]})
        job.write({'state': 'accepted', 'payload': payload,
            'payload_hash': hashlib.sha256(payload.encode()).hexdigest(),
            'attempts': 1, 'attempted_at': fields.Datetime.now(), 'submission_uuid': 'old-submission'})
        move.sudo().write({'e_invoice_uuid': 'rejected-receipt', 'e_invoice_status': 'Submitted'})
        rejected = {'uuid': 'rejected-receipt', 'internalId': move.name, 'status': 'Invalid',
            'document': original, 'validationResults': {'errorCode': 'NI310'}}
        job._apply_remote(rejected)
        self.assertFalse(job.safe_to_retry)
        self.assertTrue(job.attempted_at)
        corrected = dict(original, receiver={'id': 'corrected-id'}, signatures=[{'value': 'new-signature'}])
        client = MagicMock(company=self.company)
        def prepare(row):
            body = json.dumps({'documents': [corrected]})
            row.write({'payload': body, 'payload_hash': hashlib.sha256(body.encode()).hexdigest()})
        def request(method, path, **kwargs):
            if method == 'POST':
                self.assertEqual(path, '/api/v1/documentsubmissions')
                self.assertEqual(job.state, 'unknown')
                self.assertEqual(job.attempts, 2)
                self.assertEqual(json.loads(kwargs['data'])['documents'][0], corrected)
                return {'acceptedDocuments': [{'internalId': move.name, 'uuid': 'corrected-receipt'}],
                        'submissionUUID': 'new-submission'}
            self.assertEqual(method, 'GET')
            if path.endswith('/rejected-receipt/raw'):
                return rejected
            self.assertEqual(path, '/api/v1.0/documents/corrected-receipt/raw')
            return {'uuid': 'corrected-receipt', 'internalId': move.name, 'status': 'Valid', 'document': corrected}
        client.request.side_effect = request
        with self.owned_worker(client), \
                patch('odoo.addons.tradeline_eta_reliability.models.account_move.EtaClient', return_value=client), \
                patch.object(type(move), '_eta_correction_document', return_value=corrected), \
                patch.object(type(job), '_prepare', autospec=True, side_effect=prepare), \
                patch.object(type(job), '_preflight_submission', return_value=True):
            action = move.action_eta_send_now()
            result = move.action_eta_execute_staff_request(action['params']['request_id'])
            self.assertEqual(job.state, 'accepted', result)
            self.assertEqual(move.e_invoice_uuid, 'corrected-receipt')
            action = move.action_eta_send_now()
            move.action_eta_execute_staff_request(action['params']['request_id'])
        self.assertEqual(job.state, 'valid')
        self.assertEqual(job.attempts, 2)
        self.assertEqual(sum(call.args[0] == 'POST' for call in client.request.call_args_list), 1)
        archived = self.env['ir.attachment'].sudo().search([
            ('res_model', '=', 'account.move'), ('res_id', '=', move.id),
            ('name', '=', 'ETA_rejected_rejected-receipt.json')])
        self.assertEqual(json.loads(archived.raw), rejected)
        self.assertEqual(move.amount_total, 100)

    def test_company_eta_cooldown_applies_to_other_invoice_and_transport(self):
        job = self.invoice().eta_job_ids
        deadline = fields.Datetime.now() + timedelta(minutes=3)
        self.company.eta_authority_retry_at = deadline
        self.assertFalse(job._staff_prepare_send())
        from ..models.client import EtaClient
        client = EtaClient(self.company)
        try:
            with self.assertRaises(EtaError) as caught:
                client.request('POST', '/api/v1/documentsubmissions')
            self.assertEqual(caught.exception.status, 429)
        finally:client.close()

    def test_sync_reconciles_invalid_receipt_in_review_before_corrected_send(self):
        move = self.invoice().with_user(self.clerk); job = move.eta_job_ids.sudo()
        move.sudo().write({'e_invoice_uuid': 'old-invalid', 'e_invoice_status': 'Invalid'})
        job.write({'state': 'review', 'historical_review': True, 'attempts': 1,
            'attempted_at': fields.Datetime.now(), 'payload': 'old-signed-request',
            'next_attempt': fields.Datetime.now() + timedelta(minutes=4)})
        client = MagicMock(company=self.company)
        def reconcile(row, transport):
            self.assertEqual(row.state, 'review')
            self.assertEqual(row.payload, 'old-signed-request')
            self.assertEqual(row.move_id.e_invoice_uuid, 'old-invalid')
            row.move_id.write({'e_invoice_uuid': False, 'e_invoice_sent': False, 'e_invoice_status': 'Draft'})
            row.write({'state': 'queued', 'historical_review': False, 'attempted_at': False,
                'payload': False, 'payload_hash': False, 'submission_uuid': False})
        def prepare(row):
            row.payload = json.dumps({'documents': [{'internalID': row.move_id.name,
                'dateTimeIssued': row.move_id._eta_issue_timestamp(), 'invoiceLines': [{'quantity': 1}],
                'signatures': [{'value': 'qa-only'}]}]})
        client.request.return_value = {'acceptedDocuments': [{'internalId': move.name, 'uuid': 'new-receipt'}],
            'submissionUUID': 'new-submission'}
        with self.owned_worker(client), \
                patch.object(type(move), '_eta_queue_corrected_document', side_effect=AssertionError('Premature correction')), \
                patch.object(type(job), '_refresh', autospec=True, side_effect=reconcile) as review, \
                patch.object(type(job), '_prepare', autospec=True, side_effect=prepare), \
                patch.object(type(job), '_preflight_submission', return_value=True):
            request = move.action_eta_send_now()
            move.action_eta_execute_staff_request(request['params']['request_id'])
        review.assert_called_once()
        self.assertEqual(job.state, 'accepted')
        self.assertEqual(job.attempts, 2)
        self.assertEqual(client.request.call_count, 1)
        self.assertEqual(client.request.call_args.args[0], 'POST')

    def test_sync_review_failure_keeps_old_receipt_and_never_posts(self):
        move = self.invoice(); job = move.eta_job_ids
        move.write({'e_invoice_uuid': 'old-invalid', 'e_invoice_status': 'Invalid'})
        job.write({'state': 'review', 'historical_review': True, 'attempts': 1,
            'attempted_at': fields.Datetime.now(), 'payload': 'retained-signed-request'})
        with self.owned_worker() as client, \
                patch.object(type(job), '_refresh', side_effect=EtaError('History temporarily unavailable')):
            job._staff_dispatch('send')
        self.assertEqual(job.state, 'review')
        self.assertTrue(job.historical_review)
        self.assertEqual(job.attempts, 1)
        self.assertEqual(job.payload, 'retained-signed-request')
        self.assertEqual(move.e_invoice_uuid, 'old-invalid')
        self.assertIn('History temporarily unavailable', job.last_error)
        client.request.assert_not_called()

    def test_unknown_attempt_with_older_invalid_receipt_cannot_enter_correction(self):
        move = self.invoice(); job = move.eta_job_ids
        move.write({'e_invoice_uuid': 'older-invalid', 'e_invoice_status': 'Invalid'})
        job.write({'state': 'unknown', 'attempts': 2, 'attempted_at': fields.Datetime.now(),
            'payload': 'newer-uncertain-request', 'historical_review': True})
        with patch.object(type(move), '_eta_queue_corrected_document', side_effect=AssertionError('Unknown resubmission')):
            self.assertTrue(job._staff_prepare_send())
        self.assertEqual(job.state, 'unknown')
        self.assertEqual(job.payload, 'newer-uncertain-request')
        self.assertEqual(job.attempts, 2)

    def test_direct_valid_duplicate_warning_enters_full_history_review(self):
        move = self.invoice(); job = move.eta_job_ids
        move.write({'e_invoice_uuid': 'valid-old', 'e_invoice_status': 'Valid'})
        job.write({'state': 'valid', 'last_error': 'Multiple currently Valid ETA receipts use this invoice number.'})
        self.assertTrue(job._staff_prepare_send())
        self.assertEqual(job.state, 'review'); self.assertTrue(job.historical_review)
        self.assertEqual(move.e_invoice_uuid, 'valid-old')

    def test_company_manual_lane_is_bounded_without_taking_backlog_lock(self):
        job = self.invoice().eta_job_ids
        with self.env.registry.cursor() as other:
            other.execute('SELECT pg_advisory_lock(741182,%s)', [self.company.id])
            try:
                done, message = job._staff_dispatch('send')
                self.assertFalse(done); self.assertIn('manual ETA operation', message)
            finally:
                other.execute('SELECT pg_advisory_unlock(741182,%s)', [self.company.id])

    def test_client_initialization_failure_releases_the_manual_company_lock(self):
        job = self.invoice().eta_job_ids
        with patch(MODULE + '.EtaClient', side_effect=EtaError('Select an ETA environment.')), \
                patch.object(self.env.cr, 'rollback'), patch.object(self.env.cr, 'commit'):
            with self.assertRaises(EtaError):job._staff_dispatch('send')
        with self.env.registry.cursor() as other:
            other.execute('SELECT pg_try_advisory_lock(741182,%s)', [self.company.id])
            self.assertTrue(other.fetchone()[0])
            other.execute('SELECT pg_advisory_unlock(741182,%s)', [self.company.id])

    def test_explicit_retry_can_bring_forward_local_backoff_but_preserves_authority_waits(self):
        job = self.invoice().eta_job_ids
        future = fields.Datetime.now() + timedelta(minutes=5)
        job.write({'next_attempt': future, 'last_error': 'The signing service is unavailable'})
        self.assertTrue(job._staff_prepare_send()); self.assertFalse(job.next_attempt)
        for values in ({'authority_retry_at': future}, {'authority_retry_at': False, 'duplicate_retry_at': future},
                       {'duplicate_retry_at': False, 'last_error': 'ETA returned HTTP 429.'}):
            job.write(dict(values, next_attempt=future))
            self.assertFalse(job._staff_prepare_send()); self.assertEqual(job.next_attempt, future)

    def test_unknown_attempt_is_checked_without_another_post_or_loss_of_signed_request(self):
        job = self.invoice().eta_job_ids
        payload = json.dumps({'documents': [{'internalID': job.move_id.name, 'signatures': [{'value': 'saved'}]}]})
        job.write({'state': 'unknown', 'attempts': 1, 'attempted_at': fields.Datetime.now(), 'payload': payload,
                   'next_attempt': fields.Datetime.now() + timedelta(minutes=20)})
        client = MagicMock(company=self.company)
        client.request.return_value = {'result': [], 'metadata': {'continuationToken': 'EndofResultSet'}}
        with self.owned_worker(client):
            job._staff_dispatch('send')
        self.assertEqual(job.state, 'unknown'); self.assertEqual(job.payload, payload); self.assertEqual(job.attempts, 1)
        self.assertTrue(all(call.args[0] == 'GET' for call in client.request.call_args_list))

    def test_status_check_never_prepares_or_posts_and_failed_check_preserves_schedule(self):
        move = self.invoice(); job = move.eta_job_ids
        move.write({'e_invoice_uuid': 'known', 'e_invoice_status': 'Submitted'})
        future = fields.Datetime.now() + timedelta(minutes=5)
        job.write({'state': 'accepted', 'next_attempt': future})
        client = MagicMock(company=self.company)
        client.request.side_effect = EtaError('ETA returned HTTP 503.', status=503)
        with self.owned_worker(client), patch.object(type(job), '_submit', side_effect=AssertionError('Status submitted')):
            self.assertFalse(self.Job._process_id(job.id, client, staff_operation=STAFF_STATUS))
        self.assertEqual(job.state, 'accepted'); self.assertEqual(job.next_attempt, future)
        self.assertIn('503', client.staff_error)

    def test_status_only_client_rejects_document_post_and_cancel(self):
        client = StatusClient(self.company)
        try:
            for method in ('POST', 'PUT', 'DELETE'):
                with self.assertRaisesRegex(UserError, 'cannot submit or cancel'):
                    client.request(method, '/api/v1/documentsubmissions')
        finally: client.close()

    def test_status_does_not_queue_unsent_or_clear_duplicate_review(self):
        job = self.invoice().eta_job_ids
        with self.assertRaisesRegex(UserError, 'No ETA receipt'):
            job._staff_check_status(MagicMock())
        self.assertEqual(job.state, 'queued')
        job.write({'state': 'review', 'last_error': 'Multiple currently Valid ETA receipts use this invoice number.'})
        before = job.read(['state', 'last_error', 'attempts', 'payload'])
        with self.assertRaisesRegex(UserError, 'complete receipt reconciliation'):
            job._staff_check_status(MagicMock())
        self.assertEqual(before, job.read(list(before[0])))

    def test_missing_rolled_back_cross_user_expired_and_public_intents_cannot_execute(self):
        move = self.invoice()
        with self.assertRaisesRegex(UserError, 'undo click'):
            with self.env.cr.savepoint():
                rolled_back = move.action_eta_send_now()['params']['request_id']
                raise UserError('undo click')
        with self.owned_worker(), self.assertRaises(AccessError):
            move.action_eta_execute_staff_request(rolled_back)
        action = move.with_user(self.clerk).action_eta_send_now()
        request = self.env['ir.attachment'].browse(action['params']['request_id'])
        original = json.loads(request.raw)
        for change in ({'uid': -1}, {'company': -1}, {'database': 'copied-stage'}, {'invoice': -1},
                       {'expires_at': fields.Datetime.to_string(fields.Datetime.now() - timedelta(seconds=1))}):
            request.raw = json.dumps(dict(original, **change)).encode()
            with self.owned_worker(), self.assertRaises(AccessError):
                move.with_user(self.clerk).action_eta_execute_staff_request(request.id)
        request.write({'raw': json.dumps(original).encode(), 'public': True})
        with self.owned_worker(), self.assertRaises(AccessError):
            move.with_user(self.clerk).action_eta_execute_staff_request(request.id)

    def test_completed_intent_cannot_be_replayed_and_copied_database_cannot_send(self):
        move = self.invoice().with_user(self.clerk)
        request = self.env['ir.attachment'].browse(move.action_eta_send_now()['params']['request_id'])
        data = json.loads(request.raw); data['state'] = 'finished'; request.raw = json.dumps(data).encode()
        with self.owned_worker(), patch.object(type(self.Job), '_staff_dispatch') as dispatch:
            result = move.action_eta_execute_staff_request(request.id)
        dispatch.assert_not_called(); self.assertIn('already started', result['title'])
        request = self.env['ir.attachment'].browse(move.action_eta_send_now()['params']['request_id'])
        self.env['ir.config_parameter'].sudo().set_param('tradeline_eta_reliability.enabled_database', 'production-other')
        with self.owned_worker(), self.assertRaisesRegex(UserError, 'not enabled'):
            move.action_eta_execute_staff_request(request.id)

    def test_readonly_user_cannot_send_and_invoice_record_rules_precede_elevation(self):
        move = self.invoice()
        readonly = new_test_user(self.env, login='eta_staff_viewer', groups='account.group_account_readonly')
        with self.assertRaises(AccessError):move.with_user(readonly).action_eta_send_now()
        rule = self.env['ir.rule'].create({'name': 'Staff QA restrict invoice',
            'model_id': self.env['ir.model']._get_id('account.move'), 'domain_force': "[('id', '!=', %s)]" % move.id})
        self.assertFalse(self.Job.with_user(self.clerk).search([('move_id', '=', move.id)]),
                         'ETA status must inherit invoice record rules, including branch restrictions')
        with self.assertRaises(AccessError):move.eta_job_ids.with_user(self.clerk).read(['staff_summary'])
        with self.assertRaises(AccessError):move.with_user(self.clerk).action_eta_send_now()
        rule.unlink()

    def test_sales_or_pos_invoice_readers_do_not_need_private_eta_job_access(self):
        move = self.invoice()
        viewer = new_test_user(self.env, login='eta_invoice_only_viewer', groups='base.group_user')
        group = self.env['res.groups'].create({'name': 'QA invoice-only reader', 'users': [Command.link(viewer.id)]})
        self.env['ir.model.access'].create({'name': 'QA invoice read without private job access',
            'model_id': self.env['ir.model']._get_id('account.move'), 'group_id': group.id,
            'perm_read': True, 'perm_write': False, 'perm_create': False, 'perm_unlink': False})
        self.assertFalse(self.Job.with_user(viewer).has_access('read'))
        result = move.with_user(viewer).read(['eta_staff_state', 'eta_staff_reason'])[0]
        self.assertEqual(result['eta_staff_state'], 'queued')
        self.assertTrue(result['eta_staff_reason'])
        with self.assertRaises(AccessError):move.with_user(viewer).action_eta_send_now()

    def test_id_review_with_zero_failures_is_visible_in_attention_search(self):
        job = self.invoice().eta_job_ids
        job.write({'state': 'review', 'failures': 0, 'last_error': 'National ID is required for this invoice.'})
        self.assertTrue(job.needs_attention)
        self.assertIn(job, self.Job.search([('id', '=', job.id), ('needs_attention', '=', True)]))
        self.assertIn('Customer identification', job.staff_summary)

    def test_earlier_approved_invoice_without_a_job_keeps_approval_and_status_does_not_queue_send(self):
        move = self.invoice()
        move.eta_job_ids.unlink()
        move.write({'e_invoice_uuid': 'earlier-approval', 'e_invoice_status': 'Valid', 'e_invoice_sent': True})
        self.assertEqual(move.eta_staff_state, 'valid')
        self.assertIn('earlier ETA receipt', move.eta_staff_reason)
        action = move.with_user(self.clerk).action_eta_check_status_now()
        self.assertEqual(action['params']['mode'], 'status')
        self.assertEqual(move.eta_job_ids.state, 'valid')
        self.assertFalse(move.eta_job_ids.attempts)

    def test_earlier_native_only_approval_is_not_downgraded_by_status_enrollment(self):
        move = self.invoice()
        move.eta_job_ids.unlink()
        move.write({'e_invoice_status': 'Valid', 'l10n_eg_uuid': 'earlier-native-approval'})
        self.assertEqual(move.eta_staff_state, 'valid')
        move.with_user(self.clerk).action_eta_check_status_now()
        self.assertEqual(move.eta_job_ids.state, 'valid')
        self.assertFalse(move.e_invoice_uuid)

    def test_staff_views_support_invoice_search_badges_and_branch_grouping(self):
        move = self.invoice(); model = self.Job.with_user(self.clerk)
        rows = model.read_group([('id', '=', move.eta_job_ids.id)], ['journal_id'], ['journal_id'])
        self.assertEqual(rows[0]['journal_id'][0], self.journal.id)
        search = etree.fromstring(model.get_view(view_id=self.env.ref('tradeline_eta_reliability.eta_job_search').id,
            view_type='search')['arch'])
        self.assertTrue(search.xpath("//field[@name='invoice_number']"))
        form = etree.fromstring(move.with_user(self.clerk).get_view(view_type='form')['arch'])
        self.assertTrue(form.xpath("//button[@name='action_eta_send_now']"))
        self.assertTrue(form.xpath("//field[@name='eta_staff_state'][@invisible='1']"))
        self.assertTrue(form.xpath("//header/button[@name='action_eta_send_now'][@string='Sync']"))
        self.assertFalse(form.xpath("//header/button[@name='action_eta_check_status_now']"))
        self.assertFalse(form.xpath("//header/button[@name='action_eta_correct_vat']"))
        self.assertTrue(form.xpath("//page//button[@name='action_eta_check_status_now'][@string='Update Status']"))
        self.assertTrue(form.xpath("//div[@role='status'][.//field[@name='eta_staff_state']]"))
        self.assertTrue(form.xpath("//field[@name='eta_staff_reason']"))
        self.assertTrue(form.xpath("//field[@name='eta_staff_next_step']"))
        self.assertTrue(form.xpath("//button[@name='action_eta_status_details']"))
        self.assertFalse(form.xpath("//button[@name='action_eta_review_saved_vat']"))

    def test_familiar_customer_invoice_list_has_status_and_direct_row_operations(self):
        ordinary = new_test_user(self.env, login='eta_old_list_invoice_clerk',
            groups='account.group_account_invoice,tradeline_eta_reliability.group_eta_workspace', company_id=self.company.id,
            company_ids=[Command.set(self.company.ids)])
        self.assertFalse(ordinary.has_group('account.group_account_manager'))
        self.assertFalse(ordinary.has_group('base.group_system'))
        view_id = self.env.ref('account.view_out_invoice_tree').id
        view = etree.fromstring(self.env['account.move'].with_user(ordinary).get_view(
            view_id=view_id, view_type='list')['arch'])
        status = view.xpath("//list/field[@name='eta_staff_state']")
        self.assertEqual(len(status), 1)
        self.assertFalse(status[0].get('optional'), 'Status must be visible without changing optional columns.')
        self.assertEqual(status[0].getprevious().get('name'), 'name')
        self.assertEqual(len(view.xpath("//list/button[@name='action_eta_send_now'][@string='Sync']")), 1)
        self.assertFalse(view.xpath("//list/button[@name='action_eta_check_status_now']"))
        self.assertTrue(view.xpath("//field[@name='eta_staff_reason'][@optional='hide']"))
        viewer = new_test_user(self.env, login='eta_old_list_readonly', groups='account.group_account_readonly')
        readonly = etree.fromstring(self.env['account.move'].with_user(viewer).get_view(
            view_id=view_id, view_type='list')['arch'])
        self.assertFalse(readonly.xpath("//field[@name='eta_staff_state']"))
        self.assertFalse(readonly.xpath("//button[@name='action_eta_send_now']"))

    def test_native_eta_notices_hide_without_changing_document_history(self):
        move = self.invoice()
        native = self.env['account.edi.document'].create({'move_id': move.id,
            'edi_format_id': self.env.ref('l10n_eg_edi_eta.edi_eg_eta').id,
            'state': 'to_send', 'error': 'Queue owns this invoice', 'blocking_level': 'info'})
        before = native.read(['state', 'error', 'blocking_level', 'attachment_id'])
        self.assertTrue(move.eta_native_edi_only)
        self.assertTrue(move.eta_native_pending_only)
        self.assertTrue(move.eta_native_error_only)
        self.assertEqual(move.edi_state, 'to_send', 'The display change must not invent native delivery.')
        self.assertEqual(before, native.read(['state', 'error', 'blocking_level', 'attachment_id']))
        form = etree.fromstring(move.with_user(self.clerk).get_view(view_type='form')['arch'])
        process = form.xpath("//button[@name='button_process_edi_web_services']/..")[0]
        self.assertIn('eta_native_pending_only', process.get('invisible'))
        for alert in form.xpath("//div[@role='alert'][.//field[@name='edi_error_message']]"):
            self.assertIn('eta_native_error_only', alert.get('invisible'))

    def test_cached_native_process_and_retry_use_the_same_durable_staff_send(self):
        move = self.invoice()
        native = self.env['account.edi.document'].create({'move_id': move.id,
            'edi_format_id': self.env.ref('l10n_eg_edi_eta.edi_eg_eta').id,
            'state': 'to_send', 'error': 'Keep original native message', 'blocking_level': 'info'})
        original_error = native.error
        for name in ('button_process_edi_web_services', 'action_retry_edi_documents_error'):
            result = getattr(move.with_user(self.clerk), name)()
            self.assertEqual(result['tag'], 'tradeline_eta_staff_operation')
            self.assertEqual(result['params']['mode'], 'send')
        self.assertEqual(move.eta_job_ids.state, 'queued')
        self.assertFalse(move.eta_job_ids.attempts)
        self.assertEqual(native.error, original_error)

    def test_cached_native_button_checks_an_accepted_invoice_instead_of_resending(self):
        move = self.invoice()
        self.env['account.edi.document'].create({'move_id': move.id,
            'edi_format_id': self.env.ref('l10n_eg_edi_eta.edi_eg_eta').id, 'state': 'to_send'})
        move.eta_job_ids._apply_remote({'uuid': 'already-received', 'internalId': move.name,
                                      'status': 'Submitted'})
        result = move.with_user(self.clerk).button_process_edi_web_services()
        self.assertEqual(result['params']['mode'], 'status')
        self.assertEqual(move.eta_job_ids.state, 'accepted')
        self.assertFalse(move.eta_job_ids.attempts)

    def test_other_edi_notices_and_processing_are_preserved(self):
        move = self.invoice()
        eta = self.env.ref('l10n_eg_edi_eta.edi_eg_eta')
        other = self.env['account.edi.format'].create({'name': 'Other EDI QA', 'code': 'other_controls_qa'})
        self.env['account.edi.document'].create([
            {'move_id': move.id, 'edi_format_id': eta.id, 'state': 'to_send',
             'error': 'ETA note', 'blocking_level': 'info'},
            {'move_id': move.id, 'edi_format_id': other.id, 'state': 'to_send',
             'error': 'Other EDI note', 'blocking_level': 'info'}])
        with patch.object(type(eta), '_needs_web_services', return_value=True):
            move.invalidate_recordset(['eta_native_edi_only', 'eta_native_pending_only', 'eta_native_error_only'])
            self.assertFalse(move.eta_native_edi_only)
            self.assertFalse(move.eta_native_pending_only)
            self.assertFalse(move.eta_native_error_only)
        parent = 'odoo.addons.account_edi.models.account_move.AccountMove.button_process_edi_web_services'
        with patch(parent, return_value='other-edi-result') as process:
            self.assertEqual(move.button_process_edi_web_services(), 'other-edi-result')
            process.assert_called_once()

    def test_cached_native_cancellation_cannot_become_a_new_submission(self):
        move = self.invoice()
        self.env['account.edi.document'].create({'move_id': move.id,
            'edi_format_id': self.env.ref('l10n_eg_edi_eta.edi_eg_eta').id, 'state': 'to_cancel'})
        with self.assertRaisesRegex(UserError, 'cancellation request'):
            move.with_user(self.clerk).button_process_edi_web_services()
        self.assertFalse(move.eta_job_ids.attempts)

    def test_separate_accounting_root_keeps_invoicing_hidden_even_if_reenabled(self):
        accounting = self.env.ref('accountant.menu_accounting', raise_if_not_found=False)
        if not accounting:
            accounting = self.env['ir.ui.menu'].create({'name': 'QA existing Accounting'})
            self.env['ir.model.data'].create({'module': 'accountant', 'name': 'menu_accounting',
                'model': 'ir.ui.menu', 'res_id': accounting.id})
        menu = self.env['ir.ui.menu']; menu._eta_place_staff_menu()
        invoicing = self.env.ref('account.menu_finance')
        self.assertFalse(invoicing.active)
        self.assertEqual(self.env.ref('tradeline_eta_reliability.menu_account_eta_jobs').parent_id, accounting)
        invoicing.active = True
        for debug in (False, True):
            self.assertNotIn(invoicing.id, menu.with_user(self.clerk)._visible_menu_ids(debug=debug))


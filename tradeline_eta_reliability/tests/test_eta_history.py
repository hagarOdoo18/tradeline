import json
from datetime import timedelta
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user


@tagged('post_install', '-at_install')
class TestEtaHistory(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.receivable = cls.env['account.account'].create({'name': 'History receivable', 'code': 'HISAR',
            'account_type': 'asset_receivable', 'reconcile': True, 'company_ids': [Command.set(cls.company.ids)]})
        cls.income = cls.env['account.account'].create({'name': 'History income', 'code': 'HISINC',
            'account_type': 'income', 'company_ids': [Command.set(cls.company.ids)]})
        cls.customer = cls.env['res.partner'].create({'name': 'History customer', 'email': 'history@example.invalid',
            'property_account_receivable_id': cls.receivable.id})
        cls.journal = cls.env['account.journal'].create({'name': 'History sales', 'code': 'HIS',
            'type': 'sale', 'edi_format_ids': [Command.clear()]})
        cls.clerk = new_test_user(cls.env, login='eta_history_clerk',
            groups='account.group_account_invoice,tradeline_eta_reliability.group_eta_workspace',
            company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)])

    def setUp(self):
        super().setUp()
        guard = patch('requests.sessions.Session.request', side_effect=AssertionError('History must not call HTTP'))
        guard.start(); self.addCleanup(guard.stop)
        self.move = self.env['account.move'].with_context(eta_queue_cron=True).create({
            'move_type': 'out_invoice', 'partner_id': self.customer.id, 'journal_id': self.journal.id,
            'invoice_date': fields.Date.today(), 'invoice_line_ids': [Command.create({
                'name': 'History item', 'account_id': self.income.id, 'quantity': 1,
                'price_unit': 100, 'tax_ids': [Command.clear()]})]})
        self.move.action_post()
        self.job = self.move.eta_job_ids
        self.subtype = self.env.ref('tradeline_eta_reliability.mt_eta_history')

    def notes(self):
        return self.env['mail.message'].search([('model', '=', 'account.move'),
            ('res_id', '=', self.move.id), ('subtype_id', '=', self.subtype.id)], order='id')

    def test_attempt_is_not_reported_as_success_and_never_notifies_followers(self):
        self.move.message_subscribe(partner_ids=self.customer.ids, subtype_ids=self.subtype.ids)
        self.job.write({'state': 'unknown', 'attempts': 1, 'attempted_at': fields.Datetime.now()})
        note = self.notes()[-1]
        self.assertIn('Submission attempt started', note.body)
        self.assertIn('not confirmed', note.body)
        self.assertTrue(note.subtype_id.internal)
        self.assertFalse(note.notification_ids)
        self.assertFalse(note.partner_ids)
        self.assertFalse(self.env['mail.mail'].search([('mail_message_id', 'in', self.notes().ids)]))

    def test_cf313_survives_date_approval_with_clear_explanation(self):
        error = json.dumps({'code': 'CF313', 'message': 'Issuance date time value is out of range',
            'propertyPath': 'document.datetimeissued'})
        self.job.write({'state': 'blocked', 'last_error': error, 'safe_to_retry': True,
            'payload': json.dumps({'documents': [{'dateTimeIssued': '2026-11-02T16:34:11Z'}]})})
        note = self.notes()[-1]
        self.assertIn('CF313', note.body)
        self.assertIn('allowed submission window', note.body)
        self.assertIn('02 Nov 2026 18:34:11', note.body)
        self.job.action_approve_current_issue_date()
        self.assertIn('Current ETA issue date approved', self.notes()[-1].body)
        self.assertIn('original Odoo invoice date', self.notes()[-1].body)
        self.assertIn('CF313', self.notes()[-2].body)

    def test_repeated_uncertain_poll_does_not_spam_history(self):
        self.job.write({'state': 'unknown', 'attempts': 1})
        self.job._delay('Submission outcome needs reconciliation. No duplicate was sent.', state='unknown')
        before = len(self.notes())
        for index in range(3):
            self.job._delay('Submission outcome needs reconciliation. No duplicate was sent.', seconds=900, state='unknown')
            self.job.checked_at = fields.Datetime.now() + timedelta(seconds=index)
        self.assertEqual(len(self.notes()), before)
        self.assertIn('receipt verification is pending', self.notes()[-1].body)

    def test_remote_received_and_approved_are_distinct_and_rejection_is_one_note(self):
        data = {'uuid': 'history-receipt', 'internalId': self.move.name, 'status': 'Submitted'}
        self.job._apply_remote(data)
        self.assertIn('Received by ETA', self.notes()[-1].body)
        self.job._apply_remote(dict(data, status='Valid'))
        self.assertIn('Approved by ETA', self.notes()[-1].body)
        before = len(self.notes())
        self.job._apply_remote(dict(data, status='Invalid', validationResults={
            'errorCode': 'CV302', 'error': 'Product code is invalid'}))
        self.assertEqual(len(self.notes()), before + 1)
        self.assertIn('CV302', self.notes()[-1].body)

    def test_untrusted_error_is_escaped_and_history_is_available_to_invoice_clerk(self):
        self.job.write({'state': 'blocked', 'last_error': '<script>alert(1)</script> customer ID error'})
        self.assertNotIn('<script>', self.notes()[-1].body)
        history = self.job.with_user(self.clerk).eta_history_ids
        self.assertEqual(set(history.ids), set(self.notes().ids))
        self.assertTrue(history.with_user(self.clerk).read(['body']))
        self.assertTrue(all(note.model == 'account.move' and note.res_id == self.move.id for note in history))

    def test_existing_job_first_update_records_previous_state_honestly(self):
        # Simulate an existing job at upgrade, without inventing past events.
        self.job.flush_recordset(['history_event_key'])
        self.env.cr.execute('UPDATE tradeline_eta_job SET history_event_key=NULL WHERE id=%s', [self.job.id])
        self.job.invalidate_recordset(['history_event_key'])
        self.move.e_invoice_issue = '{"code":"CF313","message":"Old issue timestamp rejected"}'
        self.job.write({'state': 'unknown', 'attempts': 1})
        self.assertIn('Previous recorded ETA status', self.notes()[-2].body)
        self.assertIn('Earlier invoice message', self.notes()[-2].body)
        self.assertIn('CF313', self.notes()[-2].body)
        self.assertIn('Submission attempt started', self.notes()[-1].body)

    def test_vat_approval_is_recorded_without_submitting(self):
        self.move.with_user(self.clerk)._eta_approve_vat_bypass('Documented exemption <approved>')
        note = self.notes()[-1]
        self.assertIn('Sending without VAT approved', note.body)
        self.assertIn('Documented exemption', note.body)
        self.assertNotIn('<approved>', note.body)
        self.assertFalse(self.job.attempts)
        self.assertFalse(note.notification_ids)

    def test_history_reader_checks_invoice_company_access(self):
        other = self.env['res.company'].create({'name': 'Other history company',
            'activity_code_id': self.company.activity_code_id.id,
            'type_code_id': self.company.type_code_id.id, 'sub_type_code_id': self.company.sub_type_code_id.id})
        reader = self.job.with_user(self.clerk).with_context(allowed_company_ids=other.ids)
        reader.invalidate_recordset(['eta_history_ids'])
        with self.assertRaises(AccessError):
            reader.eta_history_ids

    def test_history_refreshes_after_new_event_without_reopening_record(self):
        before = self.job.eta_history_ids
        self.job.write({'state': 'blocked', 'last_error': 'Customer ID needs correction'})
        self.assertEqual(len(self.job.eta_history_ids), len(before) + 1)

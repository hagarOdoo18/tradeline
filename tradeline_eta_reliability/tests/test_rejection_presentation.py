import hashlib
import json
from datetime import timedelta
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user


@tagged('post_install', '-at_install')
class TestEtaRejectionPresentation(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        values = {'vat':'123456789'}
        for name,value in [('company_size','small'),('company_device',1)]:
            if name in cls.company.partner_id._fields:
                values[name] = value
        cls.company.partner_id.with_context(no_vat_validation=True).write(values)
        cls.company.write({'client_id':'rejection-presentation-qa','client_secret':'qa-only-secret','config_type':'uat'})
        cls.env.ref('base.EGP').active = True
        cls.receivable = cls.env['account.account'].create({'name':'Rejection QA receivable','code':'RJPAR',
            'account_type':'asset_receivable','reconcile':True,'company_ids':[Command.set(cls.company.ids)]})
        cls.income = cls.env['account.account'].create({'name':'Rejection QA income','code':'RJPINC',
            'account_type':'income','company_ids':[Command.set(cls.company.ids)]})
        cls.partner = cls.env['res.partner'].create({'name':'Rejection QA buyer',
            'property_account_receivable_id':cls.receivable.id})
        cls.product = cls.env['product.product'].create({'name':'Rejection QA product'})
        cls.journal = cls.env['account.journal'].create({'name':'Rejection QA sales','code':'RJP',
            'type':'sale','company_id':cls.company.id,'edi_format_ids':[Command.clear()]})
        cls.clerk = new_test_user(cls.env, login='eta_rejection_presentation_clerk', groups='account.group_account_invoice',
            company_id=cls.company.id,company_ids=[Command.set(cls.company.ids)])

    def setUp(self):
        super().setUp()
        guard = patch('requests.sessions.Session.request', side_effect=AssertionError('Presentation tried real HTTP'))
        guard.start(); self.addCleanup(guard.stop)

    def review_job(self):
        move = self.env['account.move'].with_context(eta_queue_cron=True).create({
            'move_type':'out_invoice','journal_id':self.journal.id,'partner_id':self.partner.id,
            'currency_id':self.env.ref('base.EGP').id,'invoice_date':fields.Date.today(),
            'invoice_line_ids':[Command.create({'name':'Rejection QA item','product_id':self.product.id,
                'account_id':self.income.id,'quantity':1,'price_unit':100,'tax_ids':[Command.clear()]})]})
        move.action_post()
        move.eta_job_ids.sudo().write({'state':'review','historical_review':True})
        return move.eta_job_ids.sudo()

    def retained(self, job):
        attempted = fields.Datetime.now()-timedelta(minutes=10)
        payload = json.dumps({'documents':[{'internalID':job.move_id.name,'signatures':[{'value':'retained-qa'}]}]})
        job.write({'attempts':1,'attempted_at':attempted,'payload':payload,
            'payload_hash':hashlib.sha256(payload.encode()).hexdigest()})
        return attempted

    def rejection(self, job, latest=False):
        reason = json.dumps({'status':'Invalid','validationSteps':[{'error':{'errorCode':'CV302',
            'error':'ItemCode is not approved','propertyPath':'invoiceLines.itemCode'}}]})
        job.move_id.write({'e_invoice_status':'Invalid','e_invoice_uuid':'current-rejected-receipt',
            'e_invoice_issue':reason,'e_invoice_sent':True})
        job.last_error = reason
        if latest:
            attempted = self.retained(job)
            job.write({'submission_uuid':'current-rejected-submission','checked_at':fields.Datetime.now()})
            job.move_id.e_invoice_date = attempted

    def test_legacy_confirmed_invalid_receipt_is_rejected_for_staff_but_history_stays_held(self):
        job = self.review_job(); self.rejection(job)
        before = job.read(['state','historical_review','attempts','attempted_at','payload','payload_hash','safe_to_retry'])[0]
        financial = job.move_id.read(['amount_total','amount_tax','invoice_date','date','state'])[0]
        self.assertEqual(job.staff_state,'invalid')
        self.assertIn('earlier submission',job.staff_summary)
        self.assertIn('Then use Sync invoice',job.required_action)
        self.assertEqual(job.read(list(before))[0],before)
        self.assertEqual(job.move_id.read(list(financial))[0],financial)

    def test_latest_final_rejection_uses_submission_and_chronology_proof(self):
        job = self.review_job(); self.rejection(job,latest=True)
        self.assertEqual(job.staff_state,'invalid')
        self.assertIn('product code',job.staff_summary)
        self.assertIn('checks existing ETA receipts',job.required_action)
        self.assertEqual(job.state,'review'); self.assertTrue(job.historical_review)

    def test_old_invalid_receipt_cannot_hide_a_newer_unconfirmed_attempt(self):
        job = self.review_job(); self.rejection(job,latest=True)
        job.move_id.e_invoice_date = job.attempted_at-timedelta(days=1)
        self.assertEqual(job.staff_state,'review')
        job.state = 'unknown'
        self.assertEqual(job.staff_state,'unknown')
        self.assertEqual(job.attempts,1)

    def test_conflicting_receipts_or_missing_latest_submission_proof_stay_review(self):
        job = self.review_job(); self.rejection(job)
        job.move_id.l10n_eg_uuid = 'another-receipt'
        self.assertEqual(job.staff_state,'review')
        job.move_id.l10n_eg_uuid = False
        self.retained(job)
        job.checked_at = fields.Datetime.now()
        self.assertEqual(job.staff_state,'review','A check timestamp alone is not an outcome for the retained attempt')

    def test_exact_safe_intake_rejection_is_visible_but_generic_errors_are_not_rejections(self):
        job = self.review_job(); self.retained(job)
        error = 'ETA rejected the submission (1). {"code":"CF320","message":"Wrong receiver country"}'
        job.write({'safe_to_retry':True,'last_error':error})
        job.move_id.write({'e_invoice_status':'Error','e_invoice_issue':error})
        self.assertEqual(job.staff_state,'invalid')
        self.assertIn('customer country',job.staff_summary)
        job.move_id.e_invoice_issue = 'An unrelated old error'
        self.assertEqual(job.staff_state,'review')
        job.write({'last_error':'ETA communication failed; retry scheduled.'})
        self.assertEqual(job.staff_state,'review')

    def test_actual_missing_product_data_is_a_local_correction_not_an_eta_rejection(self):
        job = self.review_job()
        job.last_error = 'Missing One of required details [e_invoicing_code] for product [Rejection QA product] !!!'
        with patch.object(type(self.product),'e_invoicing_code',False,create=True):
            job._compute_staff_display_state()
            self.assertEqual(job.staff_state,'blocked')
            self.assertIn('not a confirmed ETA rejection',job.staff_summary)
        with patch.object(type(self.product),'e_invoicing_code','EG-QA-CORRECTED',create=True):
            job._compute_staff_display_state()
            self.assertEqual(job.staff_state,'review','Do not retain an active missing-data label after the source is fixed')
        job.write({'attempts':1})
        with patch.object(type(self.product),'e_invoicing_code',False,create=True):
            job._compute_staff_display_state()
            self.assertEqual(job.staff_state,'review','An incomplete transmission marker must stay uncertain')

    def test_backfill_changes_only_staff_labels_and_is_administrative(self):
        rejected = self.review_job(); self.rejection(rejected)
        uncertain = self.review_job(); self.rejection(uncertain,latest=True)
        uncertain.move_id.e_invoice_date = uncertain.attempted_at-timedelta(days=1)
        jobs = rejected | uncertain
        fields_to_keep = ['state','attempts','attempted_at','payload_hash','payload','historical_review','safe_to_retry']
        before = jobs.read(fields_to_keep)
        self.env.flush_all()
        self.env.cr.execute("UPDATE tradeline_eta_job SET staff_state='review' WHERE id IN %s",[tuple(jobs.ids)])
        jobs.invalidate_recordset(['staff_state'])
        result = self.env['tradeline.eta.job'].sudo()._refresh_review_staff_labels()
        self.assertGreaterEqual(result['changed'],1)
        self.assertEqual(rejected.staff_state,'invalid')
        self.assertEqual(uncertain.staff_state,'review')
        self.assertEqual(jobs.read(fields_to_keep),before)
        with self.assertRaises(AccessError):
            self.env['tradeline.eta.job'].with_user(self.clerk)._refresh_review_staff_labels()

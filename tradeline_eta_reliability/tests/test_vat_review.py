import json
from datetime import timedelta
from contextlib import ExitStack, nullcontext
from unittest.mock import MagicMock, patch

from lxml import etree
from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user

from ..models.draft_correction import EtaVatCorrectionTransport
from ..models.vat_review import document_vat
from ..models.client import EtaError


@tagged('post_install', '-at_install')
class TestEtaVatReview(TransactionCase):
    def setUp(self):
        super().setUp()
        # Other disposable fixtures can have their own ETA credentials. Keep
        # this probe fixture company-scoped while production probes all issuers.
        self.env['res.company'].sudo().search([('id', '!=', self.company.id)]).write({
            'client_id': False, 'client_secret': False})
        # Keep the real VAT gate; identity/history have their own full suites.
        guard = patch.object(EtaVatCorrectionTransport, '_preflight_submission', return_value=True)
        guard.start()
        self.addCleanup(guard.stop)

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        values = {'vat': '123456789'}
        for key, value in [('company_size', 'small'), ('company_device', 1)]:
            if key in cls.company.partner_id._fields:
                values[key] = value
        cls.company.partner_id.with_context(no_vat_validation=True).write(values)
        cls.company.write({'client_id': 'vat-qa', 'client_secret': 'vat-qa', 'config_type': 'uat'})
        cls.env.ref('base.EGP').active = True
        cls.receivable = cls.env['account.account'].create({'name': 'VAT review receivable', 'code': 'VATRAR',
            'account_type': 'asset_receivable', 'reconcile': True, 'company_ids': [Command.set(cls.company.ids)]})
        cls.income = cls.env['account.account'].create({'name': 'VAT review income', 'code': 'VATRIN',
            'account_type': 'income', 'company_ids': [Command.set(cls.company.ids)]})
        cls.partner = cls.env['res.partner'].create({'name': 'VAT review buyer',
            'property_account_receivable_id': cls.receivable.id})
        cls.journal = cls.env['account.journal'].create({'name': 'VAT review', 'code': 'VATR',
            'type': 'sale', 'company_id': cls.company.id, 'edi_format_ids': [Command.clear()]})
        tax_type = cls.env['account.tax.type.code'].search([('code', '=', 'T1')], limit=1)
        tax_type = tax_type or cls.env['account.tax.type.code'].create({'code': 'T1', 'ar_name': 'VAT'})
        cls.vat = cls.env['account.tax'].create({'name': 'VAT review 14%', 'amount': 14,
            'company_id': cls.company.id, 'type_tax_use': 'sale', 'type_code_id': tax_type.id})
        cls.clerk = new_test_user(cls.env, login='eta_vat_review_clerk', groups='account.group_account_invoice',
            company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)])

    def invoice(self, vat=False, kind='out_invoice'):
        move = self.env['account.move'].with_context(eta_queue_cron=True).create({
            'move_type': kind, 'journal_id': self.journal.id, 'partner_id': self.partner.id,
            'currency_id': self.env.ref('base.EGP').id, 'invoice_date': fields.Date.today(),
            'invoice_line_ids': [Command.create({'name': 'VAT review item', 'account_id': self.income.id,
                'quantity': 1, 'price_unit': 100, 'tax_ids': [Command.set(self.vat.ids if vat else [])]})]})
        move.action_post()
        return move

    def payload(self, move, vat=0):
        return {'internalID': move.name, 'issuer': {'id': self.company.vat},
            'dateTimeIssued': move._eta_issue_timestamp(), 'invoiceLines': [{'quantity': 1}],
            'taxTotals': [{'taxType': 'T1', 'amount': vat}], 'signatures': [{'value': 'qa'}]}

    def test_missing_vat_stops_real_submit_before_post_and_durable_attempt(self):
        move = self.invoice()
        job, client = move.eta_job_ids, MagicMock()
        job.payload = json.dumps({'documents': [self.payload(move)]})
        with self.assertRaisesRegex(UserError, 'VAT is missing'):
            job._submit(client)
        client.request.assert_not_called()
        self.assertFalse(job.attempts)
        self.assertFalse(job.attempted_at)

    def test_prepared_zero_vat_payload_cannot_bypass_taxed_ledger(self):
        move = self.invoice(vat=True)
        job = move.eta_job_ids
        job.payload = json.dumps({'documents': [self.payload(move)]})
        self.assertFalse(move.eta_vat_missing)
        with self.assertRaisesRegex(UserError, 'prepared ETA document'):
            job._preflight_submission(MagicMock())

    def test_other_tax_does_not_count_as_vat(self):
        self.vat.type_code_id = False
        move = self.invoice(vat=True)
        self.assertNotEqual(move.amount_tax, 0)
        self.assertTrue(move.eta_vat_missing)

    def test_native_lowercase_vat_code_is_recognized_without_legacy_metadata(self):
        self.vat.write({'type_code_id':False,'l10n_eg_eta_code':'t1_v009'})
        move=self.invoice(vat=True)
        self.assertFalse(move.eta_vat_missing)
        self.assertNotIn(move,self.env['account.move'].search([('eta_vat_missing','=',True)]))
        job=move.eta_job_ids
        job.payload=json.dumps({'documents':[self.payload(move,14)]})
        self.assertTrue(job._preflight_submission(MagicMock()))

    def test_native_t10_fees_cannot_be_mistaken_for_t1_vat(self):
        self.vat.write({'type_code_id':False,'l10n_eg_eta_code':'t10_mn01'})
        move=self.invoice(vat=True)
        self.assertTrue(move.eta_vat_missing)
        self.assertIn(move,self.env['account.move'].search([('eta_vat_missing','=',True)]))

    def test_positive_vat_reaches_remaining_identity_and_history_checks(self):
        move = self.invoice(vat=True)
        job = move.eta_job_ids
        job.payload = json.dumps({'documents': [self.payload(move, 14)]})
        with patch.object(EtaVatCorrectionTransport, '_preflight_submission', return_value=True) as remaining:
            self.assertTrue(job._preflight_submission(MagicMock()))
        remaining.assert_called_once()

    def test_zero_vat_credit_can_reverse_original_receipt(self):
        move = self.invoice(kind='out_refund')
        self.assertFalse(move.eta_vat_missing)
        with patch.object(EtaVatCorrectionTransport, '_preflight_submission', return_value=True):
            self.assertTrue(move.eta_job_ids._preflight_submission(MagicMock()))

    def test_unknown_and_valid_receipt_checks_are_not_blocked_by_missing_vat(self):
        move = self.invoice()
        move.eta_job_ids.write({'state':'unknown','attempts':1,'attempted_at':fields.Datetime.now()})
        self.assertEqual(move.eta_vat_review_state,'receipt_review')
        move.write({'e_invoice_uuid': 'existing', 'e_invoice_status': 'Valid'})
        with patch.object(type(move.eta_job_ids), '_refresh') as refresh:
            move.eta_job_ids._submit(MagicMock())
        refresh.assert_called_once()

    def test_late_approval_reconciliation_precedes_new_submission_vat_gate(self):
        job = self.invoice().eta_job_ids
        with patch.object(EtaVatCorrectionTransport, '_preflight_submission', return_value=False):
            self.assertFalse(job._preflight_submission(MagicMock()))

    def test_connection_probe_uses_only_read_only_lookup_and_is_rate_bounded(self):
        self.env['ir.config_parameter'].sudo().set_param('tradeline_eta_reliability.enabled_database', self.env.cr.dbname)
        self.company.eta_health_checked_at = False
        client = MagicMock()
        with patch('odoo.addons.tradeline_eta_reliability.models.connection_health.EtaClient', return_value=client):
            self.company._cron_eta_connection_probe()
            self.company._cron_eta_connection_probe()
        client.request.assert_called_once_with('GET', '/api/v1.0/documenttypes')
        self.assertEqual(self.company.eta_health_state, 'ok')

    def test_copied_database_cannot_probe_eta(self):
        self.env['ir.config_parameter'].sudo().set_param('tradeline_eta_reliability.enabled_database', 'production-not-this-copy')
        with patch('odoo.addons.tradeline_eta_reliability.models.connection_health.EtaClient') as client:
            self.company._cron_eta_connection_probe()
        client.assert_not_called()

    def test_eta_cooldown_probe_waits_without_http_and_snapshot_marks_stale(self):
        self.env['ir.config_parameter'].sudo().set_param('tradeline_eta_reliability.enabled_database', self.env.cr.dbname)
        self.company.write({'eta_health_checked_at':False,'eta_authority_retry_at':fields.Datetime.now()+timedelta(minutes=2)})
        with patch('odoo.addons.tradeline_eta_reliability.models.connection_health.EtaClient') as client:
            self.company._cron_eta_connection_probe()
        client.assert_not_called()
        self.assertEqual(self.company.eta_health_state, 'waiting')
        self.company.eta_health_checked_at = fields.Datetime.now()-timedelta(minutes=4)
        result = self.env['tradeline.eta.job'].with_user(self.clerk).get_staff_connection_status()
        self.assertEqual(result[0]['state'], 'stale')

    def test_health_access_error_is_distinct_from_connection_error(self):
        self.env['ir.config_parameter'].sudo().set_param('tradeline_eta_reliability.enabled_database', self.env.cr.dbname)
        self.company.write({'eta_health_checked_at':False,'eta_authority_retry_at':False})
        client = MagicMock()
        client.request.side_effect = EtaError('HTTP 401', status=401)
        with patch('odoo.addons.tradeline_eta_reliability.models.connection_health.EtaClient', return_value=client):
            self.company._cron_eta_connection_probe()
        self.assertEqual(self.company.eta_health_state, 'access')

    def test_zero_duration_measurement_is_a_real_sample_not_missing_data(self):
        job = self.invoice(vat=True).eta_job_ids
        job.write({'send_response_measured_at':fields.Datetime.now(), 'send_response_seconds':0})
        rows = self.env['tradeline.eta.job'].with_user(self.clerk).get_staff_connection_status()
        self.assertGreaterEqual(rows[0]['send_count'],1)
        self.assertIsNot(rows[0]['send_seconds'],False)

    def test_rechecking_an_earlier_approval_does_not_invent_new_approval_latency(self):
        move=self.invoice(vat=True)
        move.write({'e_invoice_uuid':'earlier-approved','e_invoice_status':'Valid'})
        job=move.eta_job_ids
        job.write({'state':'valid','attempted_at':fields.Datetime.now()-timedelta(days=2)})
        job._apply_remote({'uuid':'earlier-approved','internalId':move.name,'status':'Valid'})
        self.assertFalse(job.first_approval_observed_at)
        self.assertFalse(job.approval_delay_seconds)

    def test_manual_bypass_worker_holds_missing_vat_without_post(self):
        move = self.invoice()
        job = move.eta_job_ids
        job.payload = json.dumps({'documents': [self.payload(move)]})
        client = MagicMock(company=self.company)
        client.staff_error = False
        self.env['ir.config_parameter'].sudo().set_param('tradeline_eta_reliability.enabled_database', self.env.cr.dbname)
        self.env.flush_all()
        with ExitStack() as stack:
            stack.enter_context(patch.object(self.env.registry, 'cursor', side_effect=lambda: nullcontext(self.env.cr)))
            stack.enter_context(patch.object(self.env.cr, 'commit', side_effect=self.env.flush_all))
            stack.enter_context(patch.object(self.env.cr, 'rollback', side_effect=self.env.flush_all))
            stack.enter_context(patch('odoo.addons.tradeline_eta_reliability.models.staff_workflow.EtaClient', return_value=client))
            done, message = job._staff_dispatch('send')
        self.assertTrue(done)
        self.assertEqual(job.state, 'blocked')
        self.assertIn('VAT is missing', job.last_error)
        client.request.assert_not_called()
        self.assertFalse(job.attempts)

    def test_missing_vat_search_matches_computation_and_preserves_invoice_rules(self):
        missing, taxed = self.invoice(), self.invoice(vat=True)
        self.assertIn(missing, self.env['account.move'].search([('eta_vat_missing', '=', True)]))
        self.assertNotIn(taxed, self.env['account.move'].search([('eta_vat_missing', '=', True)]))
        self.assertIn(taxed, self.env['account.move'].search([('eta_vat_missing', '=', False)]))
        rule = self.env['ir.rule'].create({'name': 'VAT QA inaccessible invoice',
            'model_id': self.env['ir.model']._get_id('account.move'), 'domain_force': "[('id','!=',%s)]" % missing.id})
        self.assertNotIn(missing.id, self.env['account.move'].with_user(self.clerk).search([('eta_vat_missing', '=', True)]).ids)
        rule.unlink()

    def test_saved_receipt_reveals_missing_vat_after_odoo_taxes_were_changed(self):
        move = self.invoice(vat=True)
        move.write({'e_invoice_uuid': 'vat-receipt', 'e_invoice_status': 'Valid'})
        doc = self.payload(move)
        attachment = self.env['ir.attachment'].create({'name': 'ETA_vat-receipt.json', 'res_model': 'account.move',
            'res_id': move.id, 'company_id': self.company.id, 'mimetype': 'application/json',
            'raw': json.dumps({'request': doc, 'response': {'l10n_eg_uuid': 'vat-receipt'}}).encode()})
        move.l10n_eg_eta_json_doc_id = attachment
        before = (move.amount_total, move.amount_tax, move.invoice_date, move.eta_job_ids.payload, move.eta_job_ids.attempts)
        move.with_user(self.clerk).action_eta_review_saved_vat()
        self.assertTrue(move.eta_receipt_vat_missing)
        self.assertEqual(move.eta_vat_review_state, 'approved_missing')
        self.assertEqual(before, (move.amount_total, move.amount_tax, move.invoice_date, move.eta_job_ids.payload, move.eta_job_ids.attempts))
        with self.assertRaises(AccessError):
            move.with_user(self.clerk).write({'eta_receipt_vat_missing': False})

    def test_wrong_receipt_identity_cannot_record_a_vat_finding(self):
        move = self.invoice()
        move.e_invoice_uuid = 'real-receipt'
        self.assertFalse(move._eta_record_receipt_vat('other-receipt', self.payload(move)))
        doc = self.payload(move)
        doc['issuer']['id'] = '987654321'
        self.assertFalse(move._eta_record_receipt_vat('real-receipt', doc))
        self.assertFalse(move.eta_receipt_vat_checked_at)

    def test_document_vat_rejects_nonfinite_other_tax_and_zero(self):
        for value in ('NaN', 'Infinity', 0, -1):
            self.assertLessEqual(document_vat({'taxTotals': [{'taxType': 'T1', 'amount': value}]}), 0)
        self.assertEqual(document_vat({'taxTotals': [{'taxType': 'T2', 'amount': 100}]}), 0)

    def test_workspace_has_all_backlog_sent_drafts_vat_and_time_grouping(self):
        for suffix in ('all_invoices', 'backlog', 'sent_history', 'draft_invoices', 'vat_issues'):
            self.assertTrue(self.env.ref('tradeline_eta_reliability.action_eta_' + suffix))
        search = self.env.ref('tradeline_eta_reliability.eta_workspace_invoice_search')
        root = etree.fromstring(self.env['account.move'].with_user(self.clerk).get_view(view_id=search.id, view_type='search')['arch'])
        for name in ('drafts', 'sent', 'untracked', 'receipt_no_vat', 'sent_day', 'sent_hour', 'invoice_day'):
            self.assertTrue(root.xpath("//filter[@name='%s']" % name))
        for group in ('state', 'e_invoice_status', 'journal_id', 'e_invoice_date:day', 'e_invoice_date:hour'):
            self.env['account.move'].with_user(self.clerk).read_group([('move_type','=','out_invoice')], [], [group])

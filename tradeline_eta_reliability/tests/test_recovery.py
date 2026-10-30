import hashlib
import json
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from ..models.client import EtaClient, EtaError
from ..models.eta_job import PROCESSING_REVISION
from ..models.historical_recovery import match_document
from ..models.processing_window import EtaStopped, check_processing_window


@tagged('post_install', '-at_install')
class TestAutomaticRecovery(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.egp = cls.env.ref('base.EGP'); cls.usd = cls.env.ref('base.USD')
        cls.egp.write({'active': True, 'rounding': .001}); cls.usd.write({'active': True, 'rounding': .001})
        original_company = cls.env.company
        partner_values = {'name': 'Automatic recovery company', 'is_company': True, 'vat': '999999999',
                          'country_id': cls.env.ref('base.eg').id}
        for name, value in [('company_size', 'small'), ('company_device', 1)]:
            if name in cls.env['res.partner']._fields: partner_values[name] = value
        company_partner = cls.env['res.partner'].with_context(no_vat_validation=True).create(partner_values)
        cls.company = cls.env['res.company'].create({'name': 'Automatic recovery test company',
            'partner_id': company_partner.id, 'activity_code_id': original_company.activity_code_id.id,
            'type_code_id': original_company.type_code_id.id, 'sub_type_code_id': original_company.sub_type_code_id.id,
            'currency_id': cls.egp.id, 'client_id': 'test', 'client_secret': 'test', 'config_type': 'uat'})
        cls.env = cls.env(context=dict(cls.env.context, allowed_company_ids=cls.company.ids))
        cls.receivable = cls.env['account.account'].create({'name': 'Recovery receivable', 'code': 'ARRCV',
            'account_type': 'asset_receivable', 'reconcile': True, 'company_ids': [Command.set(cls.company.ids)]})
        cls.income = cls.env['account.account'].create({'name': 'Recovery income', 'code': 'ARINC',
            'account_type': 'income', 'company_ids': [Command.set(cls.company.ids)]})
        cls.partner = cls.env['res.partner'].create({'name': 'Recovery buyer', 'property_account_receivable_id': cls.receivable.id})
        cls.journal = cls.env['account.journal'].create({'name': 'Recovery sales', 'code': 'ARSL', 'type': 'sale',
            'company_id': cls.company.id, 'edi_format_ids': [Command.clear()]})
        cls.product = cls.env['product.product'].create({'name': 'Recovery item'})
        uom = cls.env['eta.uom'].search([('code', '=', 'D41')], limit=1) or cls.env['eta.uom'].create({'name': 'Each', 'code': 'D41'})
        cls.product.uom_id.eta_uom_id = uom
        cls.tax_group = cls.env['account.tax.group'].create({'name': 'Recovery taxes', 'company_id': cls.company.id})
        cls.vat = cls.tax(14, 'T1', 'V009', included=True)
        cls.wht = cls.tax(-1, 'T4', 'W002')
        cls.env['res.currency.rate'].create({'currency_id': cls.usd.id, 'company_id': cls.company.id,
            'name': fields.Date.today() - timedelta(days=1), 'rate': 1 / 56.05})

    @classmethod
    def tax(cls, amount, code, subcode, included=False):
        kind = cls.env['account.tax.type.code'].search([('code', '=', code)], limit=1)
        kind = kind or cls.env['account.tax.type.code'].create({'code': code, 'ar_name': code})
        sub = cls.env['account.tax.sub.type.code'].search([('code', '=', subcode)], limit=1)
        if not sub:
            vals = {'code': subcode, 'ar_name': subcode}
            if 'type_id' in cls.env['account.tax.sub.type.code']._fields: vals['type_id'] = kind.id
            sub = cls.env['account.tax.sub.type.code'].create(vals)
        return cls.env['account.tax'].create({'name': 'Recovery %s' % code, 'amount': amount, 'amount_type': 'percent',
            'tax_group_id': cls.tax_group.id,
            'type_tax_use': 'sale', 'company_id': cls.company.id, 'type_code_id': kind.id, 'sub_type_code_id': sub.id,
            'price_include_override': 'tax_included' if included else 'tax_excluded'})

    def invoice(self, currency=None, specs=None, refund=False):
        currency = currency or self.egp
        specs = specs or [(1, 100, 0, self.env['account.tax'])]
        m = self.env['account.move'].create({'move_type': 'out_refund' if refund else 'out_invoice',
            'journal_id': self.journal.id, 'currency_id': currency.id, 'partner_id': self.partner.id,
            'invoice_date': fields.Date.today() - timedelta(days=1),
            'invoice_line_ids': [Command.create({'product_id': self.product.id, 'account_id': self.income.id,
                'quantity': qty, 'price_unit': unit, 'discount': discount, 'tax_ids': [Command.set(taxes.ids)]})
                for qty, unit, discount, taxes in specs]})
        m.action_post(); return m

    def template(self, m):
        # A retained legacy-shaped request whose fiscal identity must stay intact.
        return {'internalID': m.name, 'dateTimeIssued': str(m.invoice_date) + 'T00:00:00Z', 'netAmount': 1,
            'totalAmount': 1, 'issuer': {'id': '123456789'}, 'receiver': {'type': 'P', 'id': ''},
            'invoiceLines': [{'quantity': l.quantity, 'itemCode': 'UNCHANGED', 'itemType': 'EGS',
                'internalCode': 'SAME', 'description': 'Retain this label',
                'taxableItems': [], 'valueDifference': 0, 'itemsDiscount': 0, 'totalTaxableFees': 0}
                for l in m.invoice_line_ids.filtered(lambda l: l.product_id and l.quantity)]}

    def reconcile(self, m):
        before = (m.invoice_date, m.amount_total, m.amount_tax, tuple(m.line_ids.mapped('balance')))
        template = self.template(m); identity = deepcopy(template)
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Serialization sent HTTP')):
            result = m._eta_reconcile_posted_document(template)
        self.assertEqual(before, (m.invoice_date, m.amount_total, m.amount_tax, tuple(m.line_ids.mapped('balance'))))
        for old, new in zip(identity['invoiceLines'], result['invoiceLines']):
            for key in ('quantity', 'itemCode', 'itemType', 'internalCode', 'description'):
                self.assertEqual(old[key], new[key])
        return result

    def test_wh_usd_posted_net_vat_withholding_and_total_survive_later_fx_changes(self):
        m = self.invoice(self.usd, [(245, 499, 0, self.vat | self.wht), (255, 499, 0, self.vat | self.wht)])
        self.env['res.currency.rate'].create({'currency_id': self.usd.id, 'company_id': self.company.id,
            'name': fields.Date.today(), 'rate': 1 / 99})
        doc = self.reconcile(m)
        self.assertAlmostEqual(doc['netAmount'], abs(m.amount_untaxed_signed), places=4)
        self.assertAlmostEqual(doc['totalAmount'], abs(m.amount_total_signed), places=4)
        self.assertEqual({x['unitValue']['currencyExchangeRate'] for x in doc['invoiceLines']}, {56.05})
        self.assertEqual({x['unitValue']['amountSold'] for x in doc['invoiceLines']}, {437.7193})
        self.assertEqual({x['taxType'] for x in doc['taxTotals']}, {'T1', 'T4'})
        self.assertAlmostEqual(doc['totalAmount'], 13861804.194, places=3)

    def test_egp_discount_included_tax_and_multiple_lines_use_posted_totals(self):
        m = self.invoice(specs=[(2, 114, 10, self.vat), (3, 225, 0, self.vat)])
        doc = self.reconcile(m)
        self.assertAlmostEqual(doc['netAmount'], m.amount_untaxed, places=4)
        self.assertAlmostEqual(doc['totalAmount'], m.amount_total, places=4)
        self.assertAlmostEqual(doc['invoiceLines'][0]['salesTotal'], 200, places=4)
        self.assertAlmostEqual(doc['invoiceLines'][0]['discount']['amount'], 20, places=4)

    def test_free_items_and_full_discounts_preserve_zero_totals(self):
        m = self.invoice(specs=[(1, 114, 0, self.vat), (2, 0, 0, self.vat), (1, 114, 100, self.vat)])
        doc = self.reconcile(m)
        self.assertEqual(doc['invoiceLines'][1]['unitValue']['amountEGP'], 0)
        self.assertEqual(doc['invoiceLines'][2]['discount']['amount'], 100)
        self.assertEqual(doc['invoiceLines'][2]['total'], 0)

    def test_credit_note_has_positive_eta_values_without_changing_signed_ledger(self):
        m = self.invoice(specs=[(3, 114, 0, self.vat | self.wht)], refund=True)
        doc = self.reconcile(m)
        self.assertGreater(doc['totalAmount'], 0)
        self.assertAlmostEqual(doc['totalAmount'], abs(m.amount_total_signed), places=4)

    def test_vat_rounding_residual_moves_only_within_the_same_posted_tax_total(self):
        m = self.invoice(specs=[(1, 11400, 0, self.vat), (1, 114, 0, self.vat)])
        details = m._prepare_invoice_aggregated_taxes(round_from_tax_lines=True)
        source = m.invoice_line_ids.filtered(lambda line: line.product_id)
        details['tax_details_per_record'][source[0]]['tax_details'][self.vat]['tax_amount'] += .0102
        details['tax_details_per_record'][source[1]]['tax_details'][self.vat]['tax_amount'] -= .0102
        with patch.object(type(m), '_prepare_invoice_aggregated_taxes', return_value=details): doc = self.reconcile(m)
        self.assertAlmostEqual(sum(x['amount'] for x in doc['taxTotals']), 1414, places=4)
        for line in doc['invoiceLines']:
            self.assertLessEqual(abs(line['taxableItems'][0]['amount'] - line['netTotal'] * .14), .00901)

    def test_genuinely_inconsistent_posted_vat_is_never_fabricated(self):
        m = self.invoice(specs=[(1, 11400, 0, self.vat)])
        details = m._prepare_invoice_aggregated_taxes(round_from_tax_lines=True)
        source = m.invoice_line_ids.filtered(lambda line: line.product_id)
        details['tax_details_per_record'][source]['tax_details'][self.vat]['tax_amount'] += .04
        with patch.object(type(m), '_prepare_invoice_aggregated_taxes', return_value=details), self.assertRaisesRegex(UserError, 'posted tax ledger'):
            self.reconcile(m)

    def test_progress_monitor_detects_a_healthy_scheduler_that_is_not_processing_jobs(self):
        self.invoice()
        monitor = self.env['tradeline.operations.monitor'].with_context(active_test=False).search([], limit=1)
        monitor = monitor or self.env['tradeline.operations.monitor'].create({})
        params = self.env['ir.config_parameter'].sudo(); now = fields.Datetime.now()
        params.set_param('tradeline_eta_reliability.enabled_database', self.env.cr.dbname)
        params.set_param('tradeline_eta_reliability.last_processed_at', str(now - timedelta(minutes=16)))
        self.env.ref('tradeline_eta_reliability.cron_eta_queue').write({'active': True, 'failure_count': 0, 'nextcall': now})
        self.assertIn('eta:progress', [signal['code'] for signal in monitor._probe_crons(now)])
        params.set_param('tradeline_eta_reliability.last_processed_at', str(now))
        self.assertNotIn('eta:progress', [signal['code'] for signal in monitor._probe_crons(now)])

    def test_missing_product_coverage_and_mixed_signs_stay_held(self):
        m = self.invoice()
        doc = self.template(m); doc['invoiceLines'] = []
        with self.assertRaises(UserError): m._eta_reconcile_posted_document(doc)
        negative = self.invoice(specs=[(-1, 10, 0, self.env['account.tax']), (1, 100, 0, self.env['account.tax'])])
        with self.assertRaisesRegex(UserError, 'Mixed-sign'): self.reconcile(negative)

    def ready(self):
        m = self.invoice(); j = m.eta_job_ids; doc = self.template(m)
        doc.update(netAmount=100, totalAmount=100)
        payload = json.dumps({'documents': [doc]})
        j.write({'payload': payload, 'payload_hash': hashlib.sha256(payload.encode()).hexdigest()})
        return m, j, doc

    def cutoff(self, deadline, database=None):
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('tradeline_eta_reliability.processing_cutoff_database', database or self.env.cr.dbname)
        params.set_param('tradeline_eta_reliability.processing_cutoff_utc', deadline)
        params.flush_model(['key', 'value'])

    def test_cutoff_drains_before_exact_deadline_and_does_not_restart_next_day(self):
        self.cutoff('2026-10-29 19:00:00')
        check_processing_window(self.env, now=datetime(2026, 10, 29, 18, 58, 59))
        with self.assertRaises(EtaStopped):
            check_processing_window(self.env, now=datetime(2026, 10, 29, 18, 59, 0))
        check_processing_window(self.env, now=datetime(2026, 10, 29, 18, 59, 59), drain_seconds=0)
        with self.assertRaises(EtaStopped):
            check_processing_window(self.env, now=datetime(2026, 10, 29, 19, 0, 0), drain_seconds=0)
        with self.assertRaises(EtaStopped):
            check_processing_window(self.env, now=datetime(2026, 10, 30, 8, 0, 0))

    def test_cutoff_is_database_bound_and_invalid_bound_cutoff_fails_closed(self):
        self.cutoff('not a timestamp', database='another-database')
        check_processing_window(self.env)
        self.cutoff('not a timestamp')
        with self.assertRaises(EtaStopped): check_processing_window(self.env)

    def test_cutoff_blocks_cached_authorization_signing_and_cron_without_new_work(self):
        self.cutoff('2000-01-01 00:00:00')
        client = EtaClient(self.company); client.token = 'cached'; client.token_until = float('inf')
        m = self.env['account.move'].new({'company_id': self.company.id})
        with patch('requests.sessions.Session.request', side_effect=AssertionError('HTTP after cutoff')), patch.object(type(self.env['tradeline.eta.job']), '_enroll_historical', side_effect=AssertionError('Enrollment after cutoff')):
            with self.assertRaises(EtaStopped): client._authorize()
            with self.assertRaises(EtaStopped): client.request('POST', '/api/v1/documentsubmissions', data=b'never')
            with self.assertRaises(EtaStopped): m.get_signature_value()
            self.env['tradeline.eta.job']._cron_process()
        client.close()

    def test_cutoff_is_rechecked_after_get_throttle_and_not_cached(self):
        self.cutoff('2099-01-01 00:00:00')
        client = EtaClient(self.company); client.token = 'test'
        with patch.object(client, '_authorize'), patch('odoo.addons.tradeline_eta_reliability.models.client.time.sleep', side_effect=lambda _: self.cutoff('2000-01-01 00:00:00')), patch.object(client.session, 'request', side_effect=AssertionError('HTTP after throttle passed cutoff')):
            with self.assertRaises(EtaStopped): client.request('GET', '/api/v1/documents/search')
        client.close()

    def test_cutoff_before_post_preserves_queued_payload_without_uncertain_marker(self):
        m, j, _ = self.ready(); client = MagicMock(); before = j.payload
        self.cutoff('2000-01-01 00:00:00')
        with self.assertRaises(EtaStopped): j._submit(client)
        client.request.assert_not_called()
        self.assertEqual(j.state, 'queued'); self.assertEqual(j.payload, before)
        self.assertFalse(j.attempted_at)

    def test_owner_drain_and_hard_deadline_follow_an_explicit_new_day(self):
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('tradeline_eta_reliability.processing_cutoff_database', self.env.cr.dbname)
        params.set_param('tradeline_eta_reliability.processing_cutoff_utc', False)
        params.set_param('tradeline_eta_reliability.owner_cutoff_utc', '2026-10-30 20:00:00')
        params.set_param('tradeline_eta_reliability.owner_drain_utc', '2026-10-30 19:55:00')
        params.flush_model(['key', 'value'])
        check_processing_window(self.env, now=datetime(2026, 10, 30, 19, 54, 59))
        with self.assertRaises(EtaStopped): check_processing_window(self.env, now=datetime(2026, 10, 30, 19, 55))
        check_processing_window(self.env, now=datetime(2026, 10, 30, 19, 59, 59), drain_seconds=0)
        with self.assertRaises(EtaStopped): check_processing_window(self.env, now=datetime(2026, 10, 30, 20), drain_seconds=0)
        with self.assertRaises(EtaStopped): check_processing_window(self.env, now=datetime(2026, 10, 31, 8))
        params.set_param('tradeline_eta_reliability.owner_cutoff_utc', '2026-10-31 20:00:00')
        params.set_param('tradeline_eta_reliability.owner_drain_utc', '2026-10-31 19:55:00')
        params.flush_model(['key', 'value'])
        check_processing_window(self.env, now=datetime(2026, 10, 31, 8))

    def test_cutoff_configuration_failure_is_visible_and_finance_holds_survive_planned_pause(self):
        self.invoice().eta_job_ids.write({'state': 'blocked', 'last_error': 'Finance must correct the customer ID.'})
        monitor = self.env['tradeline.operations.monitor'].with_context(active_test=False).search([], limit=1)
        monitor = monitor or self.env['tradeline.operations.monitor'].create({})
        self.cutoff('invalid')
        codes = {item['code'] for item in monitor._probe_crons(fields.Datetime.now())}
        self.assertIn('eta:cutoff', codes); self.assertIn('eta:rejected', codes)
        self.cutoff('2000-01-01 00:00:00')
        codes = {item['code'] for item in monitor._probe_crons(fields.Datetime.now())}
        self.assertNotIn('eta:cutoff', codes); self.assertNotIn('eta:scheduler', codes)
        self.assertNotIn('eta:progress', codes); self.assertIn('eta:rejected', codes)

    def test_stopping_after_the_durable_attempt_marker_never_authorizes_a_resubmission(self):
        _m, j, _ = self.ready(); client = MagicMock()
        self.cutoff('2099-01-01 00:00:00')
        client.request.side_effect = EtaStopped('Owner stop')
        with patch.object(self.env.cr, 'commit'), self.assertRaisesRegex(EtaStopped, '^Owner stop$'): j._submit(client)
        client.request.assert_called_once()
        self.assertEqual(j.state, 'unknown'); self.assertTrue(j.attempted_at)
        self.assertFalse(j.safe_to_retry); self.assertFalse(j.duplicate_retry_at)

    def test_explicit_duplicate_response_waits_for_its_deadline_without_losing_attempt(self):
        m, j, _ = self.ready(); client = MagicMock()
        client.request.side_effect = EtaError('ETA returned HTTP 422.', 422, 420, code='DuplicateSubmission')
        with patch.object(self.env.cr, 'commit'): j._submit(client)
        self.assertEqual(j.state, 'unknown'); self.assertTrue(j.attempted_at)
        self.assertEqual(j.duplicate_payload_hash, j.payload_hash)
        self.assertGreater(j.duplicate_retry_at, fields.Datetime.now() + timedelta(seconds=400))
        client.request.reset_mock(); j._refresh(client); client.request.assert_not_called()

    def test_unknown_422_or_transport_failure_never_gets_a_safe_duplicate_retry(self):
        _m, j, _ = self.ready(); client = MagicMock(); client.request.side_effect = EtaError('Unknown 422', 422)
        with patch.object(self.env.cr, 'commit'):
            try: j._submit(client)
            except EtaError: pass
            else: self.fail('The unknown transport status must still propagate')
        self.assertEqual(j.state, 'unknown'); self.assertFalse(j.duplicate_retry_at)

    def duplicate(self):
        m, j, doc = self.ready()
        j.write({'state': 'unknown', 'attempted_at': fields.Datetime.now(), 'duplicate_retry_at': fields.Datetime.now() - timedelta(seconds=1), 'duplicate_payload_hash': j.payload_hash})
        proof = {'from': fields.Datetime.now() - timedelta(days=400), 'to': fields.Datetime.now() - timedelta(minutes=1), 'receipts': {}}
        return m, j, doc, proof

    def test_duplicate_reconciliation_requires_full_history_and_current_source(self):
        _m, j, _doc, _proof = self.duplicate(); client = MagicMock()
        with patch.object(type(j), '_history_snapshot', return_value=None): j._refresh(client)
        self.assertEqual(j.state, 'unknown'); client.request.assert_not_called()

    def test_expired_duplicate_absence_queues_same_signed_bytes_after_complete_proof(self):
        m, j, doc, proof = self.duplicate(); before = j.payload
        with patch.object(type(j), '_history_snapshot', return_value=proof), patch.object(type(j), '_history_candidates', return_value=[]), patch.object(type(j), '_historical_source', return_value=doc): j._refresh(MagicMock())
        self.assertEqual(j.state, 'queued'); self.assertEqual(j.payload, before)
        self.assertFalse(j.attempted_at); self.assertTrue(j.historical_submission_verified)
        self.assertFalse(m.e_invoice_uuid)

    def test_matching_accepted_duplicate_is_restored_without_another_post(self):
        m, j, doc, proof = self.duplicate(); client = MagicMock()
        receipt = {'uuid': 'original', 'internalId': m.name, 'status': 'Submitted', 'document': doc}
        with patch.object(type(j), '_history_snapshot', return_value=proof), patch.object(type(j), '_history_candidates', return_value=[receipt]): j._refresh(client)
        self.assertEqual(j.state, 'accepted'); self.assertEqual(m.e_invoice_uuid, 'original')
        client.request.assert_not_called(); self.assertFalse(j.duplicate_retry_at)

    def test_other_duplicate_receipts_and_changed_source_never_queue_a_new_copy(self):
        _m, j, doc, proof = self.duplicate()
        with patch.object(type(j), '_history_snapshot', return_value=proof), patch.object(type(j), '_history_candidates', return_value=[{'document': {'other': 1}}]): j._refresh(MagicMock())
        self.assertEqual(j.state, 'unknown')
        changed = deepcopy(doc); changed['netAmount'] = 99
        with patch.object(type(j), '_history_snapshot', return_value=proof), patch.object(type(j), '_history_candidates', return_value=[]), patch.object(type(j), '_historical_source', return_value=changed): j._refresh(MagicMock())
        self.assertEqual(j.state, 'blocked'); self.assertTrue(j.attempted_at)

    def test_only_date_only_rejection_uses_recorded_owner_policy(self):
        m, j, _ = self.ready(); self.env['ir.config_parameter'].sudo().set_param('tradeline_eta_reliability.historical_recovery_database', self.env.cr.dbname)
        client = MagicMock(); client.request.return_value = {'acceptedDocuments': [], 'rejectedDocuments': [{'internalId': m.name, 'error': {'code': '1', 'details': [{'code': 'CF313', 'message': 'date out of range'}]}}]}
        before = (m.invoice_date, m.amount_total)
        with patch.object(self.env.cr, 'commit'): j._submit(client)
        self.assertEqual(j.issue_date_policy, 'current'); self.assertEqual(j.state, 'queued')
        self.assertTrue(j.issue_date_approval_note); self.assertGreater(j.next_attempt, fields.Datetime.now() + timedelta(minutes=9))
        self.assertEqual(before, (m.invoice_date, m.amount_total))
        self.assertFalse(j._is_date_only_rejection({'details': [{'code': 'CF313'}, {'code': 'NI310'}]}))

    def test_software_hold_requeues_once_but_customer_data_errors_do_not(self):
        m = self.invoice(); j = m.eta_job_ids
        self.env['ir.config_parameter'].sudo().set_param('tradeline_eta_reliability.historical_recovery_database', self.env.cr.dbname)
        j.write({'state': 'blocked', 'last_error': 'Generated ETA net or final amount differs from posted accounting values.'})
        self.assertEqual(j._requeue_software_holds(move_ids=m.ids), 1)
        self.assertEqual(j._requeue_software_holds(move_ids=m.ids), 0)
        self.assertEqual(j.processing_revision, PROCESSING_REVISION); self.assertEqual(j.state, 'review')
        j.write({'state': 'blocked', 'last_error': 'National ID is required'})
        j.processing_revision = False
        self.assertEqual(j._requeue_software_holds(move_ids=m.ids), 0)

    def test_eta_client_retains_only_structured_error_code_for_retry_classification(self):
        client = EtaClient(self.company); response = MagicMock(); response.ok = False; response.status_code = 422
        client.token = 'test-only-token'
        response.headers = {'Retry-After': '420'}; response.json.return_value = {'error': {'code': 'DuplicateSubmission', 'message': 'private details'}}
        with patch.object(client, '_authorize'), patch.object(client.session, 'request', return_value=response):
            with self.assertRaises(EtaError) as error: client.request('POST', '/api/v1/documentsubmissions', data=b'{}')
        self.assertEqual(error.exception.code, 'DuplicateSubmission'); self.assertEqual(error.exception.retry_after, 421)
        self.assertNotIn('private details', str(error.exception)); client.close()

    def test_mixed_date_errors_do_not_starve_later_software_holds(self):
        first = self.invoice(); second = self.invoice()
        first.eta_job_ids.write({'state': 'blocked', 'last_error': json.dumps({'details': [{'code': 'CF313'}, {'code': 'NI310'}]})})
        second.eta_job_ids.write({'state': 'blocked', 'last_error': 'Generated ETA net or final amount differs from posted accounting values.'})
        self.env['ir.config_parameter'].sudo().set_param('tradeline_eta_reliability.historical_recovery_database', self.env.cr.dbname)
        jobs = self.env['tradeline.eta.job']
        self.assertEqual(jobs._requeue_software_holds(limit=1, move_ids=(first | second).ids), 0)
        self.assertEqual(first.eta_job_ids.state, 'blocked')
        self.assertEqual(jobs._requeue_software_holds(limit=1, move_ids=(first | second).ids), 1)
        self.assertEqual(second.eta_job_ids.state, 'review')

    def test_legacy_optional_zero_egp_currency_fields_do_not_block_real_approvals(self):
        old = {'invoiceLines': [{'unitValue': {'currencySold': 'EGP', 'amountEGP': 100,
            'amountSold': 0.0, 'currencyExchangeRate': 0.0}}]}
        current = deepcopy(old)
        current['invoiceLines'][0]['unitValue'].pop('amountSold')
        current['invoiceLines'][0]['unitValue'].pop('currencyExchangeRate')
        self.assertTrue(match_document(old, current))
        self.assertIn('amountSold', old['invoiceLines'][0]['unitValue'])
        old['invoiceLines'][0]['unitValue']['amountSold'] = 1
        self.assertFalse(match_document(old, current))
        old['invoiceLines'][0]['unitValue'].update(currencySold='USD', amountSold=0)
        current['invoiceLines'][0]['unitValue']['currencySold'] = 'USD'
        self.assertFalse(match_document(old, current))

    def test_expected_cooldown_is_quiet_but_an_overdue_unprocessed_cooldown_is_flagged(self):
        _m, j, _doc, _proof = self.duplicate()
        j.duplicate_retry_at = fields.Datetime.now() + timedelta(minutes=5)
        self.assertFalse(j.needs_attention)
        self.assertNotIn(j, self.env['tradeline.eta.job'].search([('needs_attention', '=', True)]))
        j.duplicate_retry_at = fields.Datetime.now() - timedelta(minutes=16)
        self.assertTrue(j.needs_attention)
        self.assertIn(j, self.env['tradeline.eta.job'].search([('needs_attention', '=', True)]))

    def test_historical_date_rejection_inside_seven_days_uses_today_after_full_receipt_check(self):
        m, j, doc = self.ready()
        j.write({'state': 'blocked', 'safe_to_retry': True, 'attempted_at': fields.Datetime.now(),
                 'historical_fingerprint': j._historical_financial_fingerprint(),
                 'last_error': 'ETA rejected (1). ' + json.dumps({'code': '1', 'details': [{'code': 'CF313'}]})})
        proof = {'from': fields.Datetime.now() - timedelta(days=400), 'to': fields.Datetime.now(), 'receipts': {}}
        with patch.object(type(j), '_historical_source', return_value=doc), patch.object(type(j), '_history_snapshot', return_value=proof), patch.object(type(j), '_history_candidates', return_value=[]): j._review_historical(MagicMock())
        self.assertEqual(j.state, 'queued'); self.assertEqual(j.issue_date_policy, 'current')
        self.assertFalse(j.attempted_at); self.assertEqual(m.invoice_date, fields.Date.today() - timedelta(days=1))

    def test_foreign_receipt_matching_compares_egp_to_posted_egp_not_usd(self):
        m = self.invoice(self.usd, [(245, 499, 0, self.vat | self.wht), (255, 499, 0, self.vat | self.wht)])
        doc = self.reconcile(m)
        with patch.object(type(m.company_id), 'vat', '123456789'), patch.object(type(self.env['product.product']), 'search_count', return_value=0):
            self.assertTrue(m.eta_job_ids._historical_matches({'document': doc}, doc))

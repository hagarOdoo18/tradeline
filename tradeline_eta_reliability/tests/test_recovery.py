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
    def setUp(self):
        super().setUp()
        # Focus existing retry/window tests on their own boundary; full
        # government preflight and duplicate integration live in test_historical.
        guard = patch.object(type(self.env['tradeline.eta.job']), '_preflight_submission', return_value=True)
        guard.start()
        self.addCleanup(guard.stop)

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
            'totalAmount': 1, 'issuer': {'id': m.company_id.vat}, 'receiver': {'type': 'P', 'id': ''},
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

    def test_wh_usd_0114_converted_tax_rounding_keeps_posted_headers_and_balances(self):
        self.env['res.currency.rate'].search([
            ('currency_id', '=', self.usd.id), ('company_id', '=', self.company.id)]).write({'rate': .0179})
        m = self.invoice(self.usd, [(1, 1499.9892, 0, self.vat | self.wht)])
        self.assertAlmostEqual(abs(m.amount_untaxed_signed), 73507.263, places=3)
        vat = abs(sum(m.line_ids.filtered(lambda l: l.tax_line_id == self.vat).mapped('balance')))
        self.assertAlmostEqual(vat, 10291.006, places=3)
        doc = self.reconcile(m)
        self.assertEqual(doc['totalAmount'], abs(m.amount_total_signed))
        self.assertEqual(doc['netAmount'], abs(m.amount_untaxed_signed))
        self.assertEqual(next(t['amount'] for t in doc['taxTotals'] if t['taxType'] == 'T1'), vat)
        line = doc['invoiceLines'][0]
        allocated = next(t['amount'] for t in line['taxableItems'] if t['taxType'] == 'T1')
        self.assertLess(abs(allocated - vat), .005)
        self.assertLessEqual(abs(allocated - line['netTotal'] * .14), .00901)
        self.assertLess(abs(line['total'] - doc['totalAmount']), .005)
        self.assertEqual(line['unitValue']['currencyExchangeRate'], 55.86592)

    def test_foreign_vat_difference_over_half_piaster_remains_blocked(self):
        m = self.invoice(self.usd, [(1, 11400, 0, self.vat)])
        details = m._prepare_invoice_aggregated_taxes(round_from_tax_lines=True)
        source = m.invoice_line_ids.filtered(lambda line: line.product_id)
        details['tax_details_per_record'][source]['tax_details'][self.vat]['tax_amount'] += .04
        with patch.object(type(m), '_prepare_invoice_aggregated_taxes', return_value=details), self.assertRaisesRegex(UserError, 'posted tax ledger'):
            self.reconcile(m)

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

    def legacy_approved_rounding(self):
        # These prices naturally create cent-booked net/VAT/gross values
        # under the existing millime currency configuration.
        m = self.invoice(specs=[(1, 2000.13, 0, self.vat), (1, 299.82, 0, self.vat)])
        current = self.reconcile(m); current['issuer'] = {'id': self.company.vat}
        old = deepcopy(current)
        for line, offset in zip(old['invoiceLines'], [.00412, .00413]):
            net = round(line['netTotal'] - offset, 5)
            vat = round(line['taxableItems'][0]['amount'] + offset, 5)
            line['unitValue'].update(amountEGP=net, amountSold=0, currencyExchangeRate=0)
            line.update(salesTotal=net, netTotal=net)
            line['taxableItems'][0]['amount'] = vat
        old.update(netAmount=round(current['netAmount'] - .00825, 5),
                   totalSalesAmount=round(current['totalSalesAmount'] - .00825, 5))
        old['taxTotals'][0]['amount'] = round(current['taxTotals'][0]['amount'] + .00825, 5)
        return m, m.eta_job_ids, current, {'status': 'Valid', 'document': old}

    def test_authority_valid_legacy_egp_rounding_restores_without_changing_posted_money(self):
        m, j, current, raw = self.legacy_approved_rounding()
        before = (m.invoice_date, m.amount_untaxed, m.amount_tax, m.amount_total, tuple(m.line_ids.mapped('balance')))
        self.assertFalse(match_document(raw['document'], current))
        self.assertTrue(j._historical_matches(raw, current))
        self.assertEqual(before, (m.invoice_date, m.amount_untaxed, m.amount_tax, m.amount_total, tuple(m.line_ids.mapped('balance'))))

    def test_approved_egp_rounding_keeps_gross_identity_quantity_codes_and_tax_rates_strict(self):
        _m, j, current, raw = self.legacy_approved_rounding()
        changes = [lambda d: d.update(totalAmount=d['totalAmount'] + .001),
            lambda d: d.update(internalID='another-invoice'),
            lambda d: d['receiver'].update(id='12345678901234'),
            lambda d: d['issuer'].update(id='123456789'),
            lambda d: d['invoiceLines'][0].update(quantity=2),
            lambda d: d['invoiceLines'][0].update(itemCode='another-code'),
            lambda d: d['invoiceLines'][0]['taxableItems'][0].update(rate=15),
            lambda d: d['invoiceLines'][0]['unitValue'].update(currencyExchangeRate=.00001)]
        for change in changes:
            changed = deepcopy(raw); change(changed['document'])
            self.assertFalse(j._historical_matches(changed, current))
        changed = deepcopy(raw); changed['document']['netAmount'] = current['netAmount'] - .009
        self.assertFalse(j._historical_matches(changed, current))

    def test_pending_invalid_and_foreign_documents_do_not_get_approved_egp_rounding(self):
        _m, j, current, raw = self.legacy_approved_rounding()
        for status in ('Submitted', 'Invalid', 'Rejected'):
            self.assertFalse(j._historical_matches(dict(raw, status=status), current))
        foreign_old = deepcopy(raw['document']); foreign_current = deepcopy(current)
        for doc in (foreign_old, foreign_current):
            for line in doc['invoiceLines']: line['unitValue']['currencySold'] = 'USD'
        self.assertFalse(match_document(foreign_old, foreign_current, approved_egp_rounding=True))

    def test_millime_booked_invoices_keep_the_original_strict_comparison(self):
        self.egp.rounding = .001
        m = self.invoice(specs=[(1, 2000, 0, self.vat), (1, 300, 0, self.vat)])
        current = self.reconcile(m); current['issuer'] = {'id': self.company.vat}
        self.assertTrue(any(round(line.balance * 100) != line.balance * 100 for line in m.line_ids))
        old = deepcopy(current); old['netAmount'] = float(round(current['netAmount'] - .006, 5))
        self.assertFalse(m.eta_job_ids._historical_matches({'status': 'Valid', 'document': old}, current))

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

    def retained_http422(self, age=900, status=422, code=None, receiver=None, automatic=False):
        m, job, doc = self.ready()
        if receiver:
            doc['receiver'] = receiver
            payload = json.dumps({'documents': [doc]})
            job.write({'payload': payload, 'payload_hash': hashlib.sha256(payload.encode()).hexdigest()})
        job.write({'state': 'unknown', 'attempts': 2,
                   'attempted_at': fields.Datetime.now() - timedelta(seconds=age)})
        error = EtaError('private response', status, code=code)
        error.eta_submission_failure = {'job': job.id, 'attempts': job.attempts,
            'attempted_at': fields.Datetime.to_string(job.attempted_at),
            'payload_sha256': job.payload_hash}
        self.assertTrue(job._preserve_uncertain_request_failure(error))
        record = self.env['ir.attachment'].search([('res_model', '=', job._name), ('res_id', '=', job.id),
            ('name', '=like', 'ETA_uncertain_request_failure_%')])
        data = json.loads(record.raw)
        data['observed_at'] = fields.Datetime.to_string(job.attempted_at + timedelta(seconds=1))
        if not automatic:
            data.pop('automatic_http422_policy', None)
        record.raw = json.dumps(data).encode()
        proof = {'from': fields.Datetime.now() - timedelta(days=400),
                 'to': fields.Datetime.now() - timedelta(minutes=1), 'receipts': {}}
        return m, job, doc, proof

    def test_new_observed_422_recovers_automatically_after_complete_proof(self):
        m, job, doc, proof = self.retained_http422(automatic=True)
        before = (m.invoice_date, m.amount_total, m.amount_tax, job.payload, job.payload_hash, job.attempts)
        with patch.object(type(job), '_history_snapshot', return_value=proof), \
                patch.object(type(job), '_history_candidates', return_value=[]), \
                patch.object(type(job), '_submission_source', return_value=doc):
            job._refresh(MagicMock())
        self.assertEqual(job.state, 'queued'); self.assertFalse(job.attempted_at)
        self.assertEqual(job._http422_automatic_count(), 1)
        self.assertEqual(before, (m.invoice_date, m.amount_total, m.amount_tax, job.payload, job.payload_hash, job.attempts))

    def test_new_explicit_duplicate_422_uses_same_bounded_recovery_and_clears_wait_markers(self):
        _m, job, doc, proof = self.retained_http422(code='DuplicateSubmission', automatic=True)
        job.write({'duplicate_retry_at': fields.Datetime.now() - timedelta(seconds=1),
                   'duplicate_payload_hash': job.payload_hash})
        with patch.object(type(job), '_history_snapshot', return_value=proof), \
                patch.object(type(job), '_history_candidates', return_value=[]), \
                patch.object(type(job), '_submission_source', return_value=doc):
            job._refresh(MagicMock())
        self.assertEqual(job.state, 'queued'); self.assertEqual(job._http422_automatic_count(), 1)
        self.assertFalse(job.duplicate_retry_at); self.assertFalse(job.duplicate_payload_hash)

    def test_automatic_422_wait_is_based_on_observed_response_and_full_retained_delay(self):
        _m, job, _doc, _proof = self.retained_http422(automatic=True)
        record = self.env['ir.attachment'].search([('res_model', '=', job._name), ('res_id', '=', job.id),
            ('name', '=like', 'ETA_uncertain_request_failure_%')])
        data = json.loads(record.raw)
        observed = fields.Datetime.now() - timedelta(seconds=30)
        data.update(observed_at=fields.Datetime.to_string(observed), retry_after_seconds=3600)
        record.raw = json.dumps(data).encode()
        with patch.object(type(job), '_history_snapshot') as search:
            job._refresh(MagicMock())
        search.assert_not_called()
        self.assertEqual(job.next_attempt, observed + timedelta(seconds=3600))
        self.assertEqual(job.state, 'unknown'); self.assertTrue(job.attempted_at)
        self.assertIn('HTTP 422', job.staff_summary)
        self.assertIn('no manual resend', job.required_action)
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('tradeline_eta_reliability.enabled_database', self.env.cr.dbname)
        cron = self.env.ref('tradeline_eta_reliability.cron_eta_queue').sudo()
        cron.write({'active': True, 'failure_count': 0, 'nextcall': fields.Datetime.now() + timedelta(minutes=1)})
        job._compute_monitoring()
        self.assertFalse(job.needs_attention)
        cron.active = False
        job._compute_monitoring()
        self.assertTrue(job.needs_attention)
        cron.write({'active': True, 'nextcall': fields.Datetime.now() - timedelta(minutes=30)})
        with patch.object(type(job), '_native_partial_progress_is_recent', return_value=False):
            job._compute_monitoring()
        self.assertTrue(job.needs_attention)

    def test_new_422_status_request_remains_get_only_even_with_automatic_policy(self):
        from ..models.staff_workflow import StatusClient
        _m, job, _doc, _proof = self.retained_http422(automatic=True)
        job.write({'next_attempt': fields.Datetime.now() + timedelta(minutes=1),
            'last_error': 'ETA returned HTTP 422.', 'failures': 2})
        before = (job.attempted_at, job.attempts, job.payload, job.payload_hash,
                  job.next_attempt, job.last_error, job.failures, job.history_event_key)
        client = StatusClient(self.company)
        client.request = MagicMock(return_value={'result': [], 'metadata': {'continuationToken': 'EndofResultSet'}})
        try:
            with patch.object(type(job), '_history_snapshot') as search:
                job._staff_check_status(client)
                job._refresh(client)
            search.assert_not_called()
            self.assertTrue(all(call.args[0] == 'GET' for call in client.request.call_args_list))
        finally: client.close()
        self.assertEqual(job.state, 'unknown')
        self.assertEqual(before, (job.attempted_at, job.attempts, job.payload, job.payload_hash,
                  job.next_attempt, job.last_error, job.failures, job.history_event_key))
        self.assertEqual(job._http422_automatic_count(), 0)

    def test_status_check_for_automatic_422_restores_receipt_without_preserving_old_wait(self):
        from ..models.staff_workflow import StatusClient
        m, job, doc, _proof = self.retained_http422(automatic=True)
        job.next_attempt = fields.Datetime.now() + timedelta(minutes=5)
        client = StatusClient(self.company)
        client.request = MagicMock(side_effect=[
            {'result': [{'internalId': m.name, 'uuid': 'status-receipt'}], 'metadata': {'continuationToken': 'EndofResultSet'}},
            {'document': doc, 'internalId': m.name, 'uuid': 'status-receipt', 'status': 'Submitted'}])
        try:job._staff_check_status(client)
        finally:client.close()
        self.assertEqual(job.state, 'accepted'); self.assertEqual(m.e_invoice_uuid, 'status-receipt')
        self.assertTrue(all(call.args[0] == 'GET' for call in client.request.call_args_list))

    def test_automatic_422_cap_stops_posts_but_still_restores_a_late_receipt(self):
        m, job, doc, _proof = self.retained_http422(code='DuplicateSubmission', automatic=True)
        job.write({'duplicate_retry_at': fields.Datetime.now() - timedelta(seconds=1),
                   'duplicate_payload_hash': job.payload_hash})
        for i in range(3):
            self.env['ir.attachment'].create({'name': 'ETA_automatic_http422_recovery_%s.json' % i,
                'res_model': job._name, 'res_id': job.id, 'company_id': job.company_id.id,
                'public': False, 'raw': b'{}'})
        before = (job.attempted_at, job.attempts, job.payload, job.payload_hash)
        client = MagicMock()
        client.request.return_value = {'result': [], 'metadata': {'continuationToken': 'EndofResultSet'}}
        job._refresh(client)
        self.assertEqual(job.state, 'unknown'); self.assertIn('Three automatic recoveries', job.last_error)
        self.assertIn('needs review', job.staff_summary)
        self.assertEqual(before, (job.attempted_at, job.attempts, job.payload, job.payload_hash))
        self.assertTrue(all(call.args[0] == 'GET' for call in client.request.call_args_list))
        client.request.side_effect = [
            {'result': [{'internalId': m.name, 'uuid': 'late-receipt'}], 'metadata': {'continuationToken': 'EndofResultSet'}},
            {'document': doc, 'internalId': m.name, 'uuid': 'late-receipt', 'status': 'Submitted'}]
        job._refresh(client)
        self.assertEqual(job.state, 'accepted'); self.assertEqual(m.e_invoice_uuid, 'late-receipt')

    def test_automatic_422_missing_proof_or_conflict_or_fiscal_change_does_not_queue(self):
        _m, job, doc, proof = self.retained_http422(automatic=True)
        with patch.object(type(job), '_history_snapshot', return_value=None):
            job._refresh(MagicMock())
        self.assertEqual(job.state, 'unknown'); self.assertEqual(job._http422_automatic_count(), 0)
        with patch.object(type(job), '_history_snapshot', return_value=proof), \
                patch.object(type(job), '_history_candidates', return_value=[{'document': {'other': 1}}]):
            job._refresh(MagicMock())
        self.assertEqual(job.state, 'unknown'); self.assertEqual(job._http422_automatic_count(), 0)
        changed = deepcopy(doc); changed['totalAmount'] += 1
        with patch.object(type(job), '_history_snapshot', return_value=proof), \
                patch.object(type(job), '_history_candidates', return_value=[]), \
                patch.object(type(job), '_submission_source', return_value=changed):
            job._refresh(MagicMock())
        self.assertEqual(job.state, 'unknown'); self.assertTrue(job.attempted_at)
        self.assertIn('source changed', job.staff_summary)
        self.assertEqual(job._http422_automatic_count(), 0)

    def test_only_new_recoverable_422_receives_automatic_policy(self):
        for status, code in ((503, None), (422, 'OtherFailure')):
            _m, job, _doc, _proof = self.retained_http422(status=status, code=code, automatic=True)
            client = MagicMock()
            client.request.return_value = {'result': [], 'metadata': {'continuationToken': 'EndofResultSet'}}
            job._refresh(client)
            self.assertEqual(job.state, 'unknown')
            self.assertTrue(all(call.args[0] == 'GET' for call in client.request.call_args_list))
            self.assertEqual(job._http422_automatic_count(), 0)

    def test_manual_422_address_correction_resigns_without_changing_fiscal_values(self):
        receiver = {'type': 'B', 'id': '123456789', 'name': 'Same business',
                    'address': {'country': 'EG', 'street': 'Old street', 'regionCity': 'Old city'}}
        m, job, doc, proof = self.retained_http422(receiver=receiver)
        old_payload = job.payload
        current = deepcopy(doc); current['receiver']['address'].update(street='Current street', regionCity='Current city')
        before = (m.invoice_date, m.amount_total, m.amount_tax, job.attempts)
        def prepare(row):
            document = dict(current, signatures=[{'value': 'fresh-qa-signature'}])
            payload = json.dumps({'documents': [document]})
            row.write({'payload': payload, 'payload_hash': hashlib.sha256(payload.encode()).hexdigest()})
        with patch.object(type(job), '_history_snapshot', return_value=proof), \
                patch.object(type(job), '_history_candidates', return_value=[]), \
                patch.object(type(job), '_submission_source', return_value=current), \
                patch.object(type(job), '_prepare', autospec=True, side_effect=prepare) as signer:
            job._staff_refresh_for_send(MagicMock())
        signer.assert_called_once(); self.assertEqual(job.state, 'queued')
        self.assertFalse(job.attempted_at); self.assertNotEqual(job.payload, old_payload)
        self.assertEqual(before, (m.invoice_date, m.amount_total, m.amount_tax, job.attempts))
        archive = self.env['ir.attachment'].search([('res_model', '=', 'account.move'),
            ('res_id', '=', m.id), ('name', '=', 'ETA_historical_recovery_%s.json' % job.id)])
        self.assertEqual(json.loads(archive.raw)['prior_job_payload'], old_payload)
        for key, value in (('id', '999999999'), ('name', 'Other business'), ('type', 'P')):
            changed = deepcopy(current); changed['receiver'][key] = value
            self.assertFalse(job._http422_address_only_change(doc, changed))
        for key, value in (('country', 'US'), ('governate', 'Other governate')):
            changed = deepcopy(current); changed['receiver']['address'][key] = value
            self.assertFalse(job._http422_address_only_change(doc, changed))
        changed = deepcopy(current); changed['totalAmount'] += .01
        self.assertFalse(job._http422_address_only_change(doc, changed))

    def test_manual_observed_422_reconciles_then_queues_same_bytes_and_preserves_money(self):
        m, job, doc, proof = self.retained_http422()
        before = (job.payload, job.payload_hash, job.attempts, m.invoice_date,
                  m.amount_total, m.amount_tax, tuple(m.line_ids.mapped('balance')))
        with patch.object(type(job), '_history_snapshot', return_value=proof), \
                patch.object(type(job), '_history_candidates', return_value=[]) as search, \
                patch.object(type(job), '_submission_source', return_value=doc):
            job._staff_refresh_for_send(MagicMock())
        search.assert_called_once()
        self.assertEqual(job.state, 'queued'); self.assertFalse(job.attempted_at)
        self.assertTrue(job.historical_submission_verified)
        self.assertEqual(before, (job.payload, job.payload_hash, job.attempts, m.invoice_date,
                  m.amount_total, m.amount_tax, tuple(m.line_ids.mapped('balance'))))
        audit = self.env['ir.attachment'].search([('res_model', '=', 'account.move'),
            ('res_id', '=', m.id), ('name', '=', 'ETA_historical_recovery_%s.json' % job.id)])
        self.assertEqual(len(audit), 1)
        saved = json.loads(audit.raw)
        self.assertEqual(saved['prior_job_payload'], before[0])
        self.assertTrue(saved['prior_attempted_at'])

    def test_manual_422_requires_cooldown_complete_history_and_unchanged_source(self):
        _m, job, doc, proof = self.retained_http422(age=60)
        with patch.object(type(job), '_history_snapshot') as history:
            job._staff_refresh_for_send(MagicMock())
        history.assert_not_called(); self.assertEqual(job.state, 'unknown')
        self.assertGreater(job.next_attempt, fields.Datetime.now() + timedelta(minutes=8))
        _m, job, doc, proof = self.retained_http422()
        with patch.object(type(job), '_history_snapshot', return_value=None):
            job._staff_refresh_for_send(MagicMock())
        self.assertEqual(job.state, 'unknown'); self.assertTrue(job.attempted_at)
        changed = deepcopy(doc); changed['totalAmount'] += 1
        with patch.object(type(job), '_history_snapshot', return_value=proof), \
                patch.object(type(job), '_history_candidates', return_value=[]), \
                patch.object(type(job), '_submission_source', return_value=changed):
            job._staff_refresh_for_send(MagicMock())
        self.assertEqual(job.state, 'unknown'); self.assertTrue(job.attempted_at)

    def test_manual_422_restores_matching_receipt_or_holds_conflicts_without_post(self):
        m, job, doc, proof = self.retained_http422(); client = MagicMock()
        receipt = {'uuid': 'existing-422-receipt', 'internalId': m.name,
                   'status': 'Submitted', 'document': doc}
        with patch.object(type(job), '_history_snapshot', return_value=proof), \
                patch.object(type(job), '_history_candidates', return_value=[receipt]):
            job._staff_refresh_for_send(client)
        self.assertEqual(job.state, 'accepted'); self.assertEqual(m.e_invoice_uuid, receipt['uuid'])
        client.request.assert_not_called()
        _m, job, doc, proof = self.retained_http422()
        with patch.object(type(job), '_history_snapshot', return_value=proof), \
                patch.object(type(job), '_history_candidates', return_value=[{'document': {'other': 1}}]):
            job._staff_refresh_for_send(client)
        self.assertEqual(job.state, 'unknown'); self.assertTrue(job.attempted_at)
        client.request.assert_not_called()

    def test_manual_422_cannot_use_timeout_different_code_or_changed_attempt_evidence(self):
        for status, code in ((503, None), (422, 'OtherFailure')):
            _m, job, _doc, _proof = self.retained_http422(status=status, code=code)
            self.assertIsNone(job._retained_http422_failure())
        _m, job, _doc, _proof = self.retained_http422()
        self.assertIsNotNone(job._retained_http422_failure())
        job.attempts += 1
        self.assertIsNone(job._retained_http422_failure())
        job.attempts -= 1
        job.payload += ' '
        self.assertIsNone(job._retained_http422_failure())

    def test_status_and_cron_do_not_release_observed_422_after_negative_search(self):
        _m, job, _doc, _proof = self.retained_http422(); client = MagicMock()
        client.request.return_value = {'result': [], 'metadata': {'continuationToken': 'EndofResultSet'}}
        before = (job.attempted_at, job.attempts, job.payload, job.payload_hash)
        job._refresh(client)
        job._staff_check_status(client)
        self.assertEqual(job.state, 'unknown')
        self.assertEqual(before, (job.attempted_at, job.attempts, job.payload, job.payload_hash))
        self.assertTrue(all(call.args[0] == 'GET' for call in client.request.call_args_list))

    def test_manual_422_honors_retained_retry_after_and_rejects_incomplete_search(self):
        _m, job, _doc, proof = self.retained_http422()
        record = self.env['ir.attachment'].search([('res_model', '=', job._name), ('res_id', '=', job.id),
            ('name', '=like', 'ETA_uncertain_request_failure_%')])
        data = json.loads(record.raw); data['retry_after_seconds'] = 3600
        record.raw = json.dumps(data).encode()
        with patch.object(type(job), '_history_snapshot') as history:
            job._staff_refresh_for_send(MagicMock())
        history.assert_not_called(); self.assertGreater(job.next_attempt, fields.Datetime.now() + timedelta(minutes=44))
        data['retry_after_seconds'] = 60; record.raw = json.dumps(data).encode()
        with patch.object(type(job), '_history_snapshot', return_value=proof), \
                patch.object(type(job), '_history_candidates', side_effect=EtaError('Incomplete pagination')):
            with self.assertRaises(EtaError): job._staff_refresh_for_send(MagicMock())
        self.assertEqual(job.state, 'unknown'); self.assertTrue(job.attempted_at)

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

    def test_eta_client_reads_root_error_code_but_does_not_infer_from_422(self):
        client = EtaClient(self.company); client.token = 'test-only-token'
        response = MagicMock(ok=False, status_code=422, headers={})
        for body, expected in (({'code': 'DuplicateSubmission', 'message': 'private'}, 'DuplicateSubmission'),
                               ({'unexpected': 'private'}, None), ([], None)):
            response.json.return_value = body
            with patch.object(client, '_authorize'), patch.object(client.session, 'request', return_value=response):
                with self.assertRaises(EtaError) as error:
                    client.request('POST', '/api/v1/documentsubmissions', data=b'{}')
            self.assertEqual(error.exception.code, expected)
            self.assertNotIn('private', str(error.exception))
        client.close()

    def test_optional_id_policy_uses_posted_egp_and_cannot_bypass_mandatory_foreign_sale_total(self):
        foreign = self.invoice(self.usd, [(1, 1000, 0, self.env['account.tax'])]); job = foreign.eta_job_ids
        doc = self.template(foreign); doc.update(totalAmount=1000, netAmount=1000)
        doc['receiver'] = {'type': 'P', 'id': '1', 'address': {'country': 'EG'}}
        self.assertGreater(abs(foreign.amount_total_signed), 25000)
        self.assertFalse(job._optional_person_id_scope(doc))

    def test_optional_id_software_recovery_is_scoped_and_still_requires_history(self):
        low = self.invoice(); high = self.invoice(specs=[(1, 25000, 0, self.env['account.tax'])])
        self.partner.country_id = self.env.ref('base.eg')
        values = {'state': 'invalid', 'processing_revision': False,
                  'last_error': json.dumps({'status': 'Invalid', 'validationSteps': [{'error': {'errorCode': 'NI310'}}]})}
        (low | high).eta_job_ids.write(values)
        self.env['ir.config_parameter'].sudo().set_param('tradeline_eta_reliability.historical_recovery_database', self.env.cr.dbname)
        with patch.object(type(self.partner), 'mobile_type', 'local', create=True), patch('requests.sessions.Session.request', side_effect=AssertionError('Requeue sent HTTP')):
            self.assertEqual(self.env['tradeline.eta.job']._requeue_software_holds(move_ids=(low | high).ids), 1)
        self.assertEqual(low.eta_job_ids.state, 'queued'); self.assertTrue(low.eta_job_ids.historical_review)
        self.assertFalse(low.eta_job_ids.historical_submission_verified)
        self.assertEqual(high.eta_job_ids.state, 'invalid')

    def test_new_optional_id_rejection_enrollment_does_not_bury_reconciliation(self):
        move = self.invoice()
        self.partner.country_id = self.env.ref('base.eg')
        if 'mobile_type' in self.partner._fields:
            self.partner.mobile_type = 'local'
        job = move.eta_job_ids
        job.write({'state': 'blocked', 'processing_revision': False, 'safe_to_retry': True,
                   'last_error': 'ETA rejected the submission (1). ' + json.dumps({'details': [{'code': 'NI310'}]})})
        self.env['ir.config_parameter'].sudo().set_param('tradeline_eta_reliability.historical_recovery_database', self.env.cr.dbname)
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Enrollment or requeue sent HTTP')):
            self.env['tradeline.eta.job']._enroll_historical(move_ids=move.ids)
            self.assertEqual(job.state, 'review')
            self.assertEqual(self.env['tradeline.eta.job']._requeue_software_holds(move_ids=move.ids), 1)
            self.assertEqual(self.env['tradeline.eta.job']._requeue_software_holds(move_ids=move.ids), 0)
        self.assertEqual(job.state, 'queued')
        self.assertTrue(job.historical_review)
        self.assertFalse(job.historical_submission_verified)
        self.assertIn('NI310', job.last_error)

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
        with patch.object(type(j), '_historical_source', return_value=doc), patch.object(type(j), '_submission_source', return_value=doc), patch.object(type(j), '_history_snapshot', return_value=proof), patch.object(type(j), '_history_candidates', return_value=[]): j._review_historical(MagicMock())
        self.assertEqual(j.state, 'queued'); self.assertEqual(j.issue_date_policy, 'current')
        self.assertFalse(j.attempted_at); self.assertEqual(m.invoice_date, fields.Date.today() - timedelta(days=1))

    def test_foreign_receipt_matching_compares_egp_to_posted_egp_not_usd(self):
        m = self.invoice(self.usd, [(245, 499, 0, self.vat | self.wht), (255, 499, 0, self.vat | self.wht)])
        doc = self.reconcile(m)
        with patch.object(type(self.env['product.product']), 'search_count', return_value=0):
            self.assertTrue(m.eta_job_ids._historical_matches({'document': doc}, doc))

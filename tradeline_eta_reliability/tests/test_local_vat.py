import hashlib
import json
from contextlib import ExitStack
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user


@tagged('post_install', '-at_install')
class TestEtaLocalVat(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.ref('base.EGP').active = True
        details = {'name': 'Local VAT QA company', 'is_company': True,
            'vat': '123456789', 'country_id': cls.env.ref('base.eg').id}
        for key, value in [('company_size', 'small'), ('company_device', 1)]:
            if key in cls.env['res.partner']._fields:
                details[key] = value
        partner = cls.env['res.partner'].with_context(no_vat_validation=True).create(details)
        cls.company = cls.env['res.company'].create({'name': 'Local VAT QA company',
            'partner_id': partner.id, 'currency_id': cls.env.ref('base.EGP').id,
            'country_id': cls.env.ref('base.eg').id,
            'activity_code_id': cls.env.company.activity_code_id.id,
            'type_code_id': cls.env.company.type_code_id.id,
            'sub_type_code_id': cls.env.company.sub_type_code_id.id})
        cls.company.write({'client_id': 'LOCAL-VAT-QA', 'client_secret': 'LOCAL-VAT-QA', 'config_type': 'uat'})
        cls.env = cls.env(context=dict(cls.env.context, allowed_company_ids=cls.company.ids))
        def account(code, name, kind, reconcile=False):
            return cls.env['account.account'].create({'code': code, 'name': name, 'account_type': kind,
                'reconcile': reconcile, 'company_ids': [Command.set(cls.company.ids)]})
        cls.receivable = account('LVAR', 'Local VAT receivable', 'asset_receivable', True)
        cls.income = account('LVIN', 'Local VAT income', 'income')
        cls.payable = account('LVTX', 'Local VAT payable', 'liability_current')
        cls.bank = account('LVBN', 'Local VAT bank', 'asset_cash')
        cls.partner = cls.env['res.partner'].create({'name': 'Local VAT QA buyer',
            'property_account_receivable_id': cls.receivable.id})
        cls.product = cls.env['product.product'].create({'name': 'Local VAT QA product'})
        cls.journal = cls.env['account.journal'].create({'name': 'Local VAT QA invoices', 'code': 'LVI',
            'company_id': cls.company.id, 'type': 'sale', 'edi_format_ids': [Command.clear()]})
        cls.general = cls.env['account.journal'].create({'name': 'Local VAT QA payments', 'code': 'LVP',
            'company_id': cls.company.id, 'type': 'general'})
        kind = cls.env['account.tax.type.code'].search([('code', '=', 'T1')], limit=1)
        kind = kind or cls.env['account.tax.type.code'].create({'code': 'T1', 'ar_name': 'VAT'})
        subtype = cls.env['account.tax.sub.type.code'].search([('code', '=', 'V009')], limit=1)
        subtype = subtype or cls.env['account.tax.sub.type.code'].create({'code': 'V009', 'ar_name': 'VAT', 'type_id': kind.id})
        group = cls.env['account.tax.group'].create({'name': 'Local VAT QA tax group',
            'company_id': cls.company.id, 'country_id': cls.env.ref('base.eg').id})
        cls.vat = cls.env['account.tax'].create({'name': 'Local VAT QA 14%', 'company_id': cls.company.id,
            'amount': 14, 'amount_type': 'percent', 'type_tax_use': 'sale', 'tax_group_id': group.id,
            'type_code_id': kind.id, 'sub_type_code_id': subtype.id, 'l10n_eg_eta_code': 't1_v009',
            'price_include_override': 'tax_included'})
        (cls.vat.invoice_repartition_line_ids | cls.vat.refund_repartition_line_ids).filtered(
            lambda line: line.repartition_type == 'tax').account_id = cls.payable
        cls.company.account_sale_tax_id = cls.vat
        cls.clerk = new_test_user(cls.env, login='eta_local_vat_clerk',
            groups='account.group_account_invoice,tradeline_eta_reliability.group_eta_workspace',
            company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)])
        cls.branch = new_test_user(cls.env, login='eta_local_vat_branch', groups='account.group_account_invoice',
            company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)])

    def setUp(self):
        super().setUp()
        guard = patch('requests.sessions.Session.request', side_effect=AssertionError('Local VAT must not use HTTP'))
        guard.start()
        self.addCleanup(guard.stop)

    def invoice(self, currency=None, rate=None):
        values = {'move_type': 'out_invoice', 'journal_id': self.journal.id, 'partner_id': self.partner.id,
            'invoice_date': fields.Date.today(), 'currency_id': (currency or self.company.currency_id).id,
            'invoice_line_ids': [Command.create({'product_id': self.product.id, 'account_id': self.income.id,
                'name': 'Original gross price', 'quantity': 1, 'price_unit': 100, 'tax_ids': [Command.clear()]})]}
        if rate:
            values['invoice_currency_rate'] = rate
        move = self.env['account.move'].with_context(eta_queue_cron=True).create(values)
        move.action_post()
        uuid = 'LOCAL-VAT-QA-%s' % move.id
        document = {'internalID': move.name, 'issuer': {'id': self.company.vat},
            'taxTotals': [{'taxType': 'T1', 'amount': 0}], 'totalAmount': abs(move.amount_total_signed)}
        attachment = self.env['ir.attachment'].create({'name': 'ETA_%s.json' % uuid,
            'res_model': 'account.move', 'res_id': move.id, 'company_id': self.company.id,
            'mimetype': 'application/json', 'raw': json.dumps({'request': document,
                'response': {'l10n_eg_uuid': uuid}}).encode()})
        move.write({'e_invoice_uuid': uuid, 'e_invoice_status': 'Valid', 'e_invoice_sent': True,
            'e_invoice_date': fields.Datetime.now(), 'l10n_eg_eta_json_doc_id': attachment.id,
            'invoice_signed': True, 'eta_submission_pending': False})
        payload = json.dumps({'documents': [document]}, separators=(',', ':'))
        move.eta_job_ids.sudo().write({'state': 'valid', 'payload': payload,
            'payload_hash': hashlib.sha256(payload.encode()).hexdigest(), 'attempts': 1,
            'attempted_at': fields.Datetime.now(), 'submission_uuid': 'LOCAL-SUBMISSION-QA',
            'historical_review': False, 'next_attempt': False})
        move._eta_record_receipt_vat(uuid, document)
        return move

    def preview(self, move, tax=None, user=None):
        return self.env['tradeline.eta.local.vat.correction'].with_user(user or self.clerk).create({
            'move_id': move.id, 'tax_id': (tax or self.vat).id})

    def pay(self, move, amount):
        payment = self.env['account.move'].create({'move_type': 'entry', 'journal_id': self.general.id,
            'date': move.date, 'line_ids': [Command.create({'account_id': self.bank.id,
                'debit': amount, 'credit': 0}), Command.create({'account_id': self.receivable.id,
                'partner_id': self.partner.id, 'debit': 0, 'credit': amount})]})
        payment.action_post()
        (move.line_ids | payment.line_ids).filtered(lambda line: line.account_id == self.receivable).reconcile()
        return payment

    def assert_apply_preserves(self, move, wizard):
        financial = move._eta_local_vat_financial_snapshot()
        transport = move._eta_local_vat_transport_snapshot()
        self.assertFalse(self.clerk.has_group('account.group_account_manager'))
        with ExitStack() as stack:
            for name in ('button_draft', 'action_post', '_post', '_eta_enqueue'):
                stack.enter_context(patch.object(type(move), name, side_effect=AssertionError('Forbidden local VAT path: '+name)))
            wizard.action_apply()
        self.assertEqual(move.state, 'posted')
        self.assertEqual(move._eta_local_vat_financial_snapshot(), financial)
        self.assertEqual(move._eta_local_vat_transport_snapshot(), transport)
        self.assertAlmostEqual(move.amount_total, 100, places=2)
        self.assertAlmostEqual(move.amount_untaxed, 87.72, places=2)
        self.assertAlmostEqual(move.amount_tax, 12.28, places=2)
        self.assertTrue(move.eta_local_vat_applied_at)
        self.assertEqual(move.eta_local_vat_applied_by, self.clerk)
        self.assertTrue(move.eta_receipt_vat_missing, 'Actual authority receipt must remain reported as zero VAT')
        self.assertFalse(move.eta_vat_correction_ids)
        self.assertFalse(move.reversal_move_ids)
        self.assertTrue(move.eta_local_vat_audit_id)
        self.assertFalse(move.eta_local_vat_audit_id.public)
        self.assertTrue(move._eta_local_vat_is_current())
        self.assertEqual(move.eta_vat_review_state, 'local_corrected')
        self.assertEqual(move.eta_vat_workflow, 'local_completed')
        self.assertIn(move, self.env['account.move'].search([('eta_vat_workflow', '=', 'local_completed')]))
        self.assertNotIn(move, self.env['account.move'].search([('eta_vat_workflow', '=', 'approved')]))
        self.assertEqual(move.eta_local_vat_audit_id.res_field, 'eta_local_vat_audit_file')
        self.assertTrue(move.with_user(self.clerk).eta_local_vat_audit_file)
        self.assertNotIn(move.eta_local_vat_audit_id, self.env['ir.attachment'].with_user(self.branch).search([
            ('res_model', '=', 'account.move'), ('res_id', '=', move.id)]))
        with self.assertRaises(AccessError):
            move.eta_local_vat_audit_id.with_user(self.branch).read(['raw'])
        with self.assertRaises(AccessError):
            move.eta_local_vat_audit_id.with_user(self.branch).unlink()
        with self.assertRaises(AccessError):
            move.with_user(self.branch).read(['eta_local_vat_audit_file', 'eta_local_vat_receipt_uuid'])
        with self.assertRaises(AccessError):
            move.eta_local_vat_audit_id.with_user(self.clerk).write({'raw': b'forged'})

    def test_local_preview_is_nonmutating_and_explicitly_keeps_customer_gross(self):
        move = self.invoice()
        before = (move._eta_local_vat_financial_snapshot(), move._eta_local_vat_transport_snapshot())
        action = move.with_user(self.clerk).action_apply_14_tax()
        wizard = self.env[action['res_model']].with_user(self.clerk).browse(action['res_id'])
        self.assertEqual(action['target'], 'new')
        self.assertEqual(wizard.original_total, 100)
        self.assertEqual(wizard.preview_total, 100)
        self.assertAlmostEqual(wizard.preview_tax, 12.28, places=2)
        self.assertEqual(wizard.preview_difference, 0)
        self.assertTrue(wizard.can_apply)
        self.assertIn('Odoo accounting only', wizard.preview_note)
        self.assertEqual((move._eta_local_vat_financial_snapshot(), move._eta_local_vat_transport_snapshot()), before)

    def test_local_unpaid_amendment_preserves_eta_and_never_calls_reset_post_or_send(self):
        move = self.invoice()
        wizard = self.preview(move)
        self.assert_apply_preserves(move, wizard)
        before = move._eta_local_vat_preview_digest()
        wizard.action_apply()  # A double click cannot create another amendment.
        self.assertEqual(move._eta_local_vat_preview_digest(), before)

    def test_local_fully_paid_and_part_paid_receivables_reconciliations_survive(self):
        for amount in (40, 100):
            with self.subTest(payment=amount):
                move = self.invoice()
                self.pay(move, amount)
                before = move.amount_residual
                self.assert_apply_preserves(move, self.preview(move))
                self.assertEqual(move.amount_residual, before)

    def test_local_foreign_currency_gross_and_original_fx_are_unchanged(self):
        usd = self.env.ref('base.USD')
        usd.active = True
        move = self.invoice(usd, .02)
        before = move.amount_total_signed
        self.assert_apply_preserves(move, self.preview(move))
        self.assertEqual(move.amount_total_signed, before)
        self.assertAlmostEqual(move.invoice_currency_rate, .02)

    def test_local_excluded_vat_that_increases_gross_is_blocked_even_when_paid(self):
        move = self.invoice()
        self.pay(move, 100)
        excluded = self.vat.copy({'name': 'Local QA excluded VAT', 'price_include_override': 'tax_excluded'})
        wizard = self.preview(move, excluded)
        self.assertEqual(wizard.preview_total, 114)
        self.assertFalse(wizard.can_apply)
        before = move._eta_local_vat_financial_snapshot()
        with self.assertRaisesRegex(UserError, 'changes the customer gross'):
            wizard.action_apply()
        self.assertEqual(move._eta_local_vat_financial_snapshot(), before)
        self.assertFalse(move.eta_local_vat_applied_at)

    def test_local_rejected_unknown_or_active_retry_history_never_uses_legacy_fallback(self):
        move = self.invoice()
        for status, state in [('Invalid', 'invalid'), ('Submitted', 'accepted'), ('Valid', 'unknown')]:
            with self.subTest(status=status, state=state):
                move.e_invoice_status = status
                move.eta_job_ids.sudo().state = state
                with patch.object(type(move), 'button_draft', side_effect=AssertionError('Legacy reset called')):
                    with self.assertRaisesRegex(UserError, 'finally approved'):
                        move.with_user(self.clerk).action_apply_14_tax()

    def test_local_cached_old_vat_wizard_routes_to_preview_without_creating_credit(self):
        move = self.invoice()
        old = self.env['tradeline.eta.vat.correction'].with_user(self.clerk).create({
            'move_id': move.id, 'tax_id': self.vat.id})
        before = (move._eta_local_vat_financial_snapshot(), move._eta_local_vat_transport_snapshot())
        action = old.with_context(_eta_legacy_credit_route=True).action_prepare()
        self.assertEqual(action['res_model'], 'tradeline.eta.local.vat.correction')
        self.assertEqual((move._eta_local_vat_financial_snapshot(), move._eta_local_vat_transport_snapshot()), before)
        self.assertFalse(move.reversal_move_ids)
        self.assertFalse(move.eta_vat_correction_ids)
        action2 = move.with_user(self.clerk).action_eta_correct_vat()
        self.assertEqual(action2['res_model'], 'tradeline.eta.local.vat.correction')

    def test_local_branch_and_forged_metadata_or_preview_capability_are_rejected(self):
        move = self.invoice()
        with self.assertRaises(AccessError):
            move.with_user(self.branch).action_eta_local_vat_preview()
        with self.assertRaises(AccessError):
            move.with_user(self.clerk).with_context(_eta_local_vat_edit=True).write({'eta_local_vat_applied_at': fields.Datetime.now()})
        with self.assertRaises(AccessError):
            self.env['tradeline.eta.local.vat.correction'].with_user(self.clerk).create({
                'move_id': move.id, 'tax_id': self.vat.id, 'preview_fingerprint': 'forged'})
        wizard = self.preview(move)
        with self.assertRaises(AccessError):
            wizard.with_context(_eta_local_vat_edit='approved').write({'applied_at': fields.Datetime.now()})

    def test_local_preview_rejects_stale_invoice_and_non_14_percent_tax(self):
        move = self.invoice()
        wizard = self.preview(move)
        move.ref = 'Changed after preview'
        with self.assertRaisesRegex(UserError, 'changed after the preview'):
            wizard.action_apply()
        wrong = self.vat.copy({'name': 'Local QA 18%', 'amount': 18})
        with self.assertRaisesRegex(UserError, '14%'):
            self.preview(move, wrong)
        self.assertEqual(move.amount_tax, 0)

    def test_local_active_credit_or_correction_blocks_without_touching_existing_documents(self):
        move = self.invoice()
        credit = move._reverse_moves([{'date': move.date, 'invoice_date': move.invoice_date}], cancel=False)
        before = credit.read(['state', 'amount_total', 'e_invoice_uuid'])[0]
        with self.assertRaisesRegex(UserError, 'already has a credit'):
            self.preview(move)
        self.assertEqual(credit.read(['state', 'amount_total', 'e_invoice_uuid'])[0], before)
        self.assertFalse(move.eta_local_vat_applied_at)

    def test_local_tax_lock_and_hash_restriction_are_not_bypassed(self):
        move = self.invoice()
        self.company.tax_lock_date = fields.Date.today()
        with self.assertRaises(UserError):
            self.preview(move)
        self.company.tax_lock_date = False
        self.journal.restrict_mode_hash_table = True
        with self.assertRaisesRegex(UserError, 'hash or audit trail'):
            self.preview(move)

    def test_local_later_amount_or_transport_change_cannot_keep_completed_classification(self):
        for change in ('amount', 'transport'):
            with self.subTest(change=change):
                move = self.invoice()
                self.assert_apply_preserves(move, self.preview(move))
                if change == 'amount':
                    # Simulate a later independent ledger adjustment. A prior
                    # audit stamp is not proof of these new amount values.
                    self.env.cr.execute('UPDATE account_move SET amount_tax=amount_tax+1, amount_untaxed=amount_untaxed-1 WHERE id=%s', [move.id])
                    move.invalidate_recordset(['amount_tax', 'amount_untaxed', 'eta_vat_review_state', 'eta_vat_workflow'])
                else:
                    move.eta_job_ids.sudo().state = 'unknown'
                self.assertFalse(move._eta_local_vat_is_current())
                self.assertNotEqual(move.eta_vat_review_state, 'local_corrected')
                self.assertNotEqual(move.eta_vat_workflow, 'local_completed')
                self.assertNotIn(move, self.env['account.move'].search([('eta_vat_workflow', '=', 'local_completed')]))

    def test_local_transport_worker_lock_blocks_amendment(self):
        move = self.invoice()
        wizard = self.preview(move)
        with self.registry.cursor() as other:
            other.execute('SELECT pg_advisory_xact_lock(741180,%s)', [move.eta_job_ids.id])
            with self.assertRaisesRegex(UserError, 'being checked by ETA'):
                wizard.action_apply()
        self.assertEqual(move.amount_tax, 0)

    def test_local_any_preservation_failure_rolls_back_entire_temporary_draft(self):
        move = self.invoice()
        wizard = self.preview(move)
        before = (move._eta_local_vat_financial_snapshot(), move._eta_local_vat_transport_snapshot())
        original = type(move)._eta_local_vat_financial_snapshot
        def changed(record):
            snapshot = original(record)
            if record.amount_tax > 0:
                snapshot['move']['amount_total'] += 1
            return snapshot
        with patch.object(type(move), '_eta_local_vat_financial_snapshot', changed):
            with self.assertRaisesRegex(UserError, 'entire amendment was rolled back'):
                wizard.action_apply()
        self.assertEqual((move._eta_local_vat_financial_snapshot(), move._eta_local_vat_transport_snapshot()), before)
        self.assertEqual(move.state, 'posted')
        self.assertEqual(move.amount_tax, 0)
        self.assertFalse(move.eta_local_vat_applied_at)

import logging
import time
from contextlib import ExitStack
from unittest.mock import patch

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from . import test_local_vat as local_vat_tests


_logger = logging.getLogger(__name__)


@tagged('post_install', '-at_install')
class TestEtaLocalVatBatch(local_vat_tests.TestEtaLocalVat):
    """Exercise the same preservation fixtures through the direct list action."""

    def direct(self, moves):
        with ExitStack() as stack:
            stack.enter_context(patch.object(type(self.env.cr), 'commit',
                side_effect=AssertionError('Direct batch committed before the request completed')))
            for method in ('button_draft', 'action_post', '_post', '_eta_enqueue', '_reverse_moves',
                           'action_eta_local_vat_preview'):
                stack.enter_context(patch.object(type(moves), method,
                    side_effect=AssertionError('Direct batch used forbidden path: ' + method)))
            return moves.with_user(self.clerk).action_apply_14_tax()

    def test_batch_paid_native_flags_apply_directly_without_transient_preview(self):
        moves = self.env['account.move']
        edi_format = self.env['account.edi.format'].search([('code', '=', 'eg_eta')], limit=1)
        before = {}
        for state, payment in (('to_send', 0), ('to_cancel', 40), ('to_send', 100)):
            move = self.invoice()
            if payment:
                self.pay(move, payment)
            self.env['account.edi.document'].create({
                'move_id': move.id, 'edi_format_id': edi_format.id, 'state': state})
            before[move.id] = (move._eta_local_vat_financial_snapshot(), move._eta_local_vat_transport_snapshot())
            moves |= move
        wizard_count = self.env['tradeline.eta.local.vat.correction'].search_count([])
        result = self.direct(moves)
        self.assertEqual(result['tag'], 'tradeline_eta_local_vat_result')
        self.assertFalse(result['params']['blocked_ids'])
        self.assertNotIn('blocked_action', result['params'])
        self.assertIn('Applied: 3.', result['params']['message'])
        self.assertIn('Blocked: 0.', result['params']['message'])
        self.assertEqual(self.env['tradeline.eta.local.vat.correction'].search_count([]), wizard_count)
        for move in moves:
            self.assertEqual((move._eta_local_vat_financial_snapshot(), move._eta_local_vat_transport_snapshot()), before[move.id])
            self.assertAlmostEqual(move.amount_tax, 12.28, places=2)
            self.assertTrue(move._eta_local_vat_is_current())
            self.assertFalse(move.reversal_move_ids | move.eta_vat_correction_ids)
        audits = moves.mapped('eta_local_vat_audit_id')
        result = self.direct(moves)
        self.assertIn('Applied: 0.', result['params']['message'])
        self.assertIn('skipped: 3.', result['params']['message'])
        self.assertEqual(moves.mapped('eta_local_vat_audit_id'), audits)

    def test_batch_failure_rolls_back_only_that_invoice_and_links_blocked_selection(self):
        first, blocked, last = self.invoice(), self.invoice(), self.invoice()
        before = (blocked._eta_local_vat_financial_snapshot(), blocked._eta_local_vat_transport_snapshot())
        original = type(first)._eta_apply_local_vat
        def fail_after_amendment(move, tax, digest):
            result = original(move, tax, digest)
            if move.id == blocked.id:
                raise UserError('QA rollback after amendment %s')
            return result
        with patch.object(type(first), '_eta_apply_local_vat', fail_after_amendment):
            result = self.direct(first | blocked | last)
        self.assertIn('Applied: 2.', result['params']['message'])
        self.assertIn('Blocked: 1.', result['params']['message'])
        self.assertIn('QA rollback after amendment', result['params']['message'])
        self.assertIn('QA rollback after amendment %s', result['params']['message'])
        self.assertEqual(result['params']['blocked_ids'], [blocked.id])
        self.assertEqual(result['params']['blocked_action']['views'], [(False, 'list'), (False, 'form')])
        domain = result['params']['blocked_action']['domain']
        self.assertEqual(domain, [('id', 'in', [blocked.id])])
        self.assertEqual(self.env['account.move'].with_user(self.clerk).search(domain).ids, [blocked.id])
        self.assertEqual((blocked._eta_local_vat_financial_snapshot(), blocked._eta_local_vat_transport_snapshot()), before)
        self.assertFalse(blocked.eta_local_vat_audit_id)
        self.assertEqual(blocked.amount_tax, 0)
        self.assertTrue(first._eta_local_vat_is_current())
        self.assertTrue(last._eta_local_vat_is_current())

    def test_batch_unresolved_or_unsent_rows_are_left_unchanged(self):
        approved, unresolved, unsent = self.invoice(), self.invoice(), self.invoice()
        unresolved.eta_job_ids.sudo().state = 'unknown'
        unsent.write({'e_invoice_uuid': False, 'e_invoice_status': False, 'e_invoice_sent': False})
        before = {move.id: move._eta_local_vat_preview_digest() for move in unresolved | unsent}
        result = self.direct((approved | unresolved | unsent).with_context(
            _eta_local_vat_edit=True, _eta_legacy_credit_route=True))
        self.assertIn('Applied: 1.', result['params']['message'])
        self.assertIn('Blocked: 2.', result['params']['message'])
        for move in unresolved | unsent:
            self.assertEqual(move._eta_local_vat_preview_digest(), before[move.id])
            self.assertFalse(move.eta_local_vat_audit_id)

    def test_batch_branch_or_inaccessible_selection_cannot_use_forged_context(self):
        allowed, hidden = self.invoice(), self.invoice()
        before = allowed._eta_local_vat_preview_digest()
        with self.assertRaises(AccessError):
            (allowed | hidden).with_user(self.branch).with_context(
                _eta_local_vat_edit=True, _eta_legacy_credit_route=True).action_eta_apply_local_vat()
        rule = self.env['ir.rule'].create({'name': 'Local VAT batch hidden invoice',
            'model_id': self.env['ir.model']._get_id('account.move'),
            'domain_force': "[('id','!=',%s)]" % hidden.id})
        try:
            with self.assertRaises(AccessError):
                (allowed | hidden).with_user(self.clerk).action_eta_apply_local_vat()
            self.assertEqual(allowed._eta_local_vat_preview_digest(), before)
        finally:
            rule.unlink()

    def test_batch_multiple_companies_select_their_own_vat_and_preserve_permissions(self):
        first = self.invoice()
        company = self.env['res.company'].create({'name': 'Local VAT batch second company',
            'currency_id': self.company.currency_id.id, 'country_id': self.company.country_id.id,
            'activity_code_id': self.company.activity_code_id.id,
            'type_code_id': self.company.type_code_id.id, 'sub_type_code_id': self.company.sub_type_code_id.id})
        self.clerk.company_ids = [Command.link(company.id)]
        original_values = {key: getattr(self, key) for key in ('env', 'company', 'receivable', 'income', 'payable', 'journal', 'vat')}
        try:
            self.env = self.env(context=dict(self.env.context, allowed_company_ids=[self.company.id, company.id]))
            self.company = company
            self.receivable = self.receivable.copy({'code': 'LVB2AR', 'company_ids': [Command.set(company.ids)]})
            self.income = self.income.copy({'code': 'LVB2IN', 'company_ids': [Command.set(company.ids)]})
            self.payable = self.payable.copy({'code': 'LVB2TX', 'company_ids': [Command.set(company.ids)]})
            self.partner.with_company(company).property_account_receivable_id = self.receivable
            self.journal = self.env['account.journal'].create({'name': 'Local VAT batch company 2',
                'code': 'LVB2', 'company_id': company.id, 'type': 'sale', 'edi_format_ids': [Command.clear()]})
            group = self.vat.tax_group_id.copy({'name': 'Local VAT batch company 2', 'company_id': company.id})
            self.vat = self.env['account.tax'].create({'name': 'Local VAT batch second 14%', 'company_id': company.id,
                'amount': 14, 'type_tax_use': 'sale', 'amount_type': 'percent', 'tax_group_id': group.id,
                'type_code_id': original_values['vat'].type_code_id.id,
                'sub_type_code_id': original_values['vat'].sub_type_code_id.id,
                'l10n_eg_eta_code': 't1_v009', 'price_include_override': 'tax_included'})
            (self.vat.invoice_repartition_line_ids | self.vat.refund_repartition_line_ids).filtered(
                lambda line: line.repartition_type == 'tax').account_id = self.payable
            company.account_sale_tax_id = self.vat
            second = self.invoice()
            result = self.direct((first | second).with_env(self.env))
            self.assertIn('Applied: 2.', result['params']['message'])
            self.assertEqual(second.invoice_line_ids.tax_ids, self.vat)
            self.assertEqual(first.invoice_line_ids.tax_ids, original_values['vat'])
        finally:
            for key, value in original_values.items():
                setattr(self, key, value)

    def test_batch_338_real_amendments_finish_within_http_budget(self):
        moves = self.env['account.move']
        for _index in range(338):
            moves |= self.invoice()
        _logger.info('Created 338 invoice fixtures; starting direct Odoo-only amendment timer')
        started = time.monotonic()
        result = self.direct(moves)
        elapsed = time.monotonic() - started
        _logger.info('Direct Odoo-only 338-invoice VAT amendment elapsed %.3fs', elapsed)
        self.assertIn('Applied: 338.', result['params']['message'])
        self.assertIn('Blocked: 0.', result['params']['message'])
        self.assertEqual(len(moves.mapped('eta_local_vat_audit_id')), 338)
        self.assertLess(elapsed, 110, 'The direct batch must leave margin below a 120-second HTTP budget.')

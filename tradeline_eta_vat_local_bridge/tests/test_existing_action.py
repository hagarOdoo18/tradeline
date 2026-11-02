from unittest.mock import patch

from odoo.tests import TransactionCase, tagged
from odoo.addons.accounting_customization.models.account_move import AccountMove as OriginalAccountingMove


@tagged('post_install', '-at_install')
class TestExistingVatActionBridge(TransactionCase):
    def test_existing_apply14_resolves_to_bridge_after_full_addon_load(self):
        method = type(self.env['account.move']).action_apply_14_tax
        self.assertTrue(method.__module__.endswith('tradeline_eta_vat_local_bridge.models.account_move'))

    def test_existing_action_history_cannot_enter_original_reset_and_post(self):
        moves = self.env['account.move'].browse([987654321, 987654322])
        expected = {'type': 'ir.actions.client', 'tag': 'tradeline_eta_local_vat_result'}
        with patch.object(type(moves), '_eta_apply14_requires_review', return_value=True), \
                patch.object(type(moves), 'action_eta_apply_local_vat', return_value=expected), \
                patch.object(OriginalAccountingMove, 'action_apply_14_tax', side_effect=AssertionError('Original historical reset route called')):
            self.assertEqual(moves.action_apply_14_tax(), expected)

    def test_truly_unsent_action_keeps_original_accounting_behavior(self):
        moves = self.env['account.move'].browse(987654321)
        expected = {'legacy_unsent_action': True}
        with patch.object(type(moves), '_eta_apply14_requires_review', return_value=False), \
                patch.object(OriginalAccountingMove, 'action_apply_14_tax', return_value=expected) as original:
            self.assertEqual(moves.action_apply_14_tax(), expected)
        original.assert_called_once()

    def test_existing_server_action_returns_direct_batch_result_instead_of_discarding_it(self):
        moves = self.env['account.move'].browse([987654321, 987654322])
        expected = {'type': 'ir.actions.client', 'tag': 'tradeline_eta_local_vat_result'}
        action = self.env.ref('accounting_customization.action_apply_14_tax_invoice').sudo()
        with patch.object(type(moves), '_eta_apply14_requires_review', return_value=True), \
                patch.object(type(moves), 'action_eta_apply_local_vat', return_value=expected), \
                patch.object(OriginalAccountingMove, 'action_apply_14_tax', side_effect=AssertionError('Original historical reset route called')):
            actual = action.with_context(active_model='account.move', active_id=moves[0].id, active_ids=moves.ids).run()
        self.assertEqual(actual, expected)

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestValuationReconciliation(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.env.user.groups_id |= cls.env.ref('stock_valuation_layer_extend.group_stock_valuation_quantity_correction')
        cls.accounts = cls.env['account.account'].create([
            {'name': name, 'code': code, 'account_type': kind, 'company_ids': [Command.set(cls.company.ids)]}
            for name, code, kind in [('Repair Stock', 'RPR001', 'asset_current'), ('Repair Difference', 'RPR002', 'expense')]])
        cls.journal = cls.env['account.journal'].create({'name': 'Repair Journal', 'code': 'RPR', 'type': 'general', 'company_id': cls.company.id})
        cls.category = cls.env['product.category'].create({
            'name': 'Reconciliation AVCO', 'property_cost_method': 'average', 'property_valuation': 'real_time',
            'property_stock_valuation_account_id': cls.accounts[0].id,
            'property_stock_account_input_categ_id': cls.accounts[1].id,
            'property_stock_account_output_categ_id': cls.accounts[1].id,
            'property_stock_journal': cls.journal.id})
        cls.product = cls.env['product.product'].create({'name': 'Repair Item', 'is_storable': True,
                                                       'categ_id': cls.category.id, 'standard_price': 100})
        cls.location = cls.env['stock.location'].create({'name': 'Repair Stock', 'usage': 'internal', 'company_id': cls.company.id})

    def setup_gap(self, physical, valued, value, lot=None):
        if physical:
            self.env['stock.quant']._update_available_quantity(self.product, self.location, physical, lot_id=lot)
        self.env['stock.valuation.layer'].create({'company_id': self.company.id, 'product_id': self.product.id,
            'lot_id': lot.id if lot else False, 'quantity': valued, 'value': value,
            'unit_cost': value / valued if valued else 0, 'remaining_qty': 0, 'remaining_value': 0,
            'description': 'Test historical discrepancy'})

    def wizard(self, policy='keep', cost=100, lot=None):
        return self.env['stock.valuation.reconciliation.wizard'].create({
            'product_id': self.product.id, 'company_id': self.company.id, 'lot_id': lot.id if lot else False,
            'confirmed': True, 'reason': 'Verified missing valuation history', 'source': 'Test count evidence',
            'value_policy': policy, 'target_unit_cost': cost, 'counterpart_account_id': self.accounts[1].id})

    def assert_result(self, wizard, quantity, value):
        before = self.env['stock.quant'].search([('product_id', '=', self.product.id)]).read(['quantity', 'reserved_quantity', 'location_id'])
        wizard.action_preview()
        wizard.action_apply()
        self.env.invalidate_all()
        self.assertEqual(before, self.env['stock.quant'].search([('product_id', '=', self.product.id)]).read(['quantity', 'reserved_quantity', 'location_id']))
        self.assertAlmostEqual(self.product.quantity_svl, quantity)
        self.assertAlmostEqual(self.product.value_svl, value)
        self.assertEqual(wizard.state, 'done')

    def test_increase_quantity_keep_value(self):
        self.setup_gap(3, 1, 300)
        w = self.wizard()
        self.assert_result(w, 3, 300)
        self.assertAlmostEqual(w.correction_layer_id.quantity, 2)
        self.assertFalse(w.correction_layer_id.account_move_id)
        self.assertAlmostEqual(self.product.standard_price, 100)
        with self.assertRaises(UserError):
            w.action_apply()

    def test_decrease_quantity_keep_value(self):
        self.setup_gap(2, 5, 200)
        w = self.wizard()
        self.assert_result(w, 2, 200)
        self.assertAlmostEqual(w.correction_layer_id.quantity, -3)

    def test_increase_value_posts_balanced_journal(self):
        self.setup_gap(3, 1, 100)
        w = self.wizard('cost')
        self.assert_result(w, 3, 300)
        move = w.correction_layer_id.account_move_id
        self.assertEqual(move.state, 'posted')
        self.assertAlmostEqual(sum(move.line_ids.mapped('balance')), 0)
        self.assertAlmostEqual(sum(move.line_ids.filtered(lambda l: l.account_id == self.accounts[0]).mapped('balance')), 200)

    def test_decrease_value_posts_credit(self):
        self.setup_gap(2, 5, 500)
        w = self.wizard('cost')
        self.assert_result(w, 2, 200)
        self.assertAlmostEqual(sum(w.correction_layer_id.account_move_id.line_ids.filtered(lambda l: l.account_id == self.accounts[0]).mapped('balance')), -300)

    def test_zero_physical_removes_orphan_value(self):
        self.setup_gap(0, 2, 200)
        self.assert_result(self.wizard('cost', cost=0), 0, 0)

    def test_stale_preview_blocks_post(self):
        self.setup_gap(3, 1, 300)
        w = self.wizard()
        w.action_preview()
        self.env['stock.quant']._update_available_quantity(self.product, self.location, 1)
        with self.assertRaises(UserError):
            w.action_apply()
        self.assertAlmostEqual(self.product.quantity_svl, 1)

    def test_confirm_and_positive_value_required(self):
        self.setup_gap(3, 1, 0)
        w = self.wizard()
        with self.assertRaises(UserError):
            w.action_preview()
        w.write({'value_policy': 'cost', 'confirmed': False})
        with self.assertRaises(UserError):
            w.action_preview()

    def test_duplicate_serial_rejected(self):
        self.product.write({'tracking': 'serial', 'lot_valuated': True})
        lot = self.env['stock.lot'].create({'name': 'REPAIR-DUP', 'product_id': self.product.id, 'company_id': self.company.id})
        self.setup_gap(2, 1, 100, lot=lot)
        with self.assertRaises(UserError):
            self.wizard(lot=lot).action_preview()

    def test_product_valued_serials_reconcile_total(self):
        self.product.write({'tracking': 'serial', 'lot_valuated': False})
        lots = self.env['stock.lot'].create([
            {'name': name, 'product_id': self.product.id, 'company_id': self.company.id}
            for name in ['REPAIR-PRODUCT-1', 'REPAIR-PRODUCT-2', 'REPAIR-PRODUCT-3']])
        for lot in lots:
            self.env['stock.quant']._update_available_quantity(self.product, self.location, 1, lot_id=lot)
        self.env['stock.valuation.layer'].create({'product_id': self.product.id, 'company_id': self.company.id,
            'quantity': 1, 'value': 300, 'remaining_qty': 0, 'remaining_value': 0})
        self.assert_result(self.wizard(), 3, 300)

    def test_product_valued_serial_duplicates_rejected(self):
        self.product.write({'tracking': 'serial', 'lot_valuated': False})
        lot = self.env['stock.lot'].create({'name': 'REPAIR-PRODUCT-DUP', 'product_id': self.product.id, 'company_id': self.company.id})
        self.setup_gap(2, 1, 100, lot=lot)
        with self.assertRaises(UserError):
            self.wizard().action_preview()

    def test_serial_missing_opening_repair_and_next_removal(self):
        self.product.write({'tracking': 'serial', 'lot_valuated': True})
        lot = self.env['stock.lot'].create({'name': 'REPAIR-OPEN', 'product_id': self.product.id, 'company_id': self.company.id})
        self.setup_gap(1, 0, 0, lot=lot)
        w = self.wizard('cost', 11499, lot=lot)
        self.assert_result(w, 1, 11499)
        self.assertAlmostEqual(lot.standard_price, 11499)
        self.product._apply_counted_inventory(self.company, self.location, 0, lot=lot, reason='Test next stock operation')
        self.assertAlmostEqual(self.product.quantity_svl, 0)
        self.assertAlmostEqual(self.product.value_svl, 0)

    def test_no_permission_rejected(self):
        self.setup_gap(3, 1, 300)
        user = self.env['res.users'].create({'name': 'Repair Viewer', 'login': 'repair_viewer_test',
            'company_id': self.company.id, 'company_ids': [Command.set(self.company.ids)],
            'groups_id': [Command.set([self.env.ref('stock.group_stock_user').id])]})
        with self.assertRaises(AccessError):
            self.wizard().with_user(user).action_preview()

    def test_regular_manager_can_open_form(self):
        manager = self.env['res.users'].create({'name': 'Repair Manager', 'login': 'repair_manager_test',
            'company_id': self.company.id, 'company_ids': [Command.set(self.company.ids)],
            'groups_id': [Command.set([self.env.ref('stock_valuation_layer_extend.group_stock_valuation_quantity_correction').id])]})
        model = self.env['stock.valuation.reconciliation.wizard'].with_user(manager)
        model.check_access('read')
        view = model.get_view(view_id=self.env.ref('stock_valuation_layer_extend.view_valuation_reconciliation_wizard').id, view_type='form')
        self.assertIn('Reconcile Valuation Only', view['arch'])

    def test_failure_rolls_back_layer_and_journal(self):
        from unittest.mock import patch
        self.setup_gap(3, 1, 100)
        w = self.wizard('cost')
        w.action_preview()
        read = type(w)._read_balances
        calls = []
        def broken_post_check(record, product):
            result = read(record, product)
            calls.append(True)
            if len(calls) == 2:
                result['quantity'] += 1
            return result
        with patch.object(type(w), '_read_balances', broken_post_check), self.assertRaises(UserError):
            w.action_apply()
        self.env.invalidate_all()
        self.assertAlmostEqual(self.product.quantity_svl, 1)
        self.assertAlmostEqual(self.product.value_svl, 100)
        self.assertFalse(self.env['stock.valuation.layer'].search([('product_id', '=', self.product.id), ('is_valuation_reconciliation', '=', True)]))
        self.assertFalse(self.env['account.move'].search([('journal_id', '=', self.journal.id)]))

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestInventoryIntegrity(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.env.user.groups_id |= cls.env.ref('stock_valuation_layer_extend.group_inventory_cost_approver')
        accounts = cls.env['account.account'].create([
            {'name': name, 'code': code, 'account_type': kind, 'company_ids': [Command.set(cls.company.ids)]}
            for name, code, kind in [('Integrity Stock', 'INT001', 'asset_current'),
                                     ('Integrity Input', 'INT002', 'asset_current'),
                                     ('Integrity Output', 'INT003', 'expense')]])
        journal = cls.env['account.journal'].create({
            'name': 'Integrity Valuation', 'code': 'IVAL', 'type': 'general', 'company_id': cls.company.id})
        category = cls.env['product.category'].create({
            'name': 'Integrity AVCO', 'property_cost_method': 'average', 'property_valuation': 'real_time',
            'property_stock_valuation_account_id': accounts[0].id,
            'property_stock_account_input_categ_id': accounts[1].id,
            'property_stock_account_output_categ_id': accounts[2].id,
            'property_stock_journal': journal.id})
        cls.product = cls.env['product.product'].create({
            'name': 'Integrity Serial', 'is_storable': True, 'tracking': 'serial',
            'lot_valuated': True, 'categ_id': category.id, 'standard_price': 100})
        cls.lot = cls.env['stock.lot'].create({
            'name': 'INTEGRITY-001', 'product_id': cls.product.id, 'company_id': cls.company.id,
            'standard_price': 100})
        cls.location = cls.env['stock.location'].create({
            'name': 'Integrity Stock', 'usage': 'internal', 'company_id': cls.company.id})

    def wizard(self, count=1, cost=100):
        return self.env['stock.count.cost.wizard'].create({
            'product_id': self.product.id, 'company_id': self.company.id,
            'location_id': self.location.id, 'lot_id': self.lot.id,
            'counted_quantity': count, 'change_cost': bool(cost), 'target_unit_cost': cost,
            'reason': 'Approved physical count', 'cost_source': 'Test approval'})

    def seed(self):
        self.product._apply_counted_inventory(self.company, self.location, 1, lot=self.lot, reason='Test opening')

    def test_count_and_revalue_posts_quantity_value_and_accounting(self):
        wizard = self.wizard(cost=11499)
        wizard.action_preview()
        wizard.action_apply()
        self.assertAlmostEqual(self.product.quantity_svl, 1)
        self.assertAlmostEqual(self.product.value_svl, 11499)
        self.assertAlmostEqual(self.lot.standard_price, 11499)
        self.assertAlmostEqual(self.lot.avg_cost, 11499)
        self.assertTrue(wizard.log_id.valuation_layer_ids)
        self.assertTrue(all(layer.account_move_id.state == 'posted'
                            for layer in wizard.log_id.valuation_layer_ids if layer.value))
        with self.assertRaises(UserError):
            wizard.action_apply()
        self.assertAlmostEqual(self.product.quantity_svl, 1)

    def test_removal_posts_matching_valuation(self):
        self.seed()
        self.product._apply_counted_inventory(self.company, self.location, 0, lot=self.lot)
        self.assertAlmostEqual(self.product.quantity_svl, 0)
        self.assertAlmostEqual(self.product.value_svl, 0)

    def test_cache_only_repair_preserves_quantity_and_value(self):
        self.seed()
        self.lot.with_context(disable_auto_svl=True).write({'standard_price': 0})
        # The issue report is a SQL view; flush the source models as a
        # committed UI request would before querying that view.
        self.env.flush_all()
        issues = self.env['stock.inventory.integrity.issue'].search([('lot_id', '=', self.lot.id)])
        self.assertTrue(issues)
        wizard = self.wizard(cost=100)
        wizard.action_preview()
        wizard.action_apply()
        self.assertAlmostEqual(self.lot.standard_price, 100)
        self.assertAlmostEqual(self.product.quantity_svl, 1)
        self.assertAlmostEqual(self.product.value_svl, 100)
        self.env.flush_all()
        self.assertFalse(self.env['stock.inventory.integrity.issue'].search([('lot_id', '=', self.lot.id)]))

    def test_missing_opening_valuation_blocks_count(self):
        self.env['stock.quant']._update_available_quantity(self.product, self.location, 1, lot_id=self.lot)
        before = len(self.product.stock_valuation_layer_ids)
        with self.assertRaises(UserError):
            self.wizard(cost=11499).action_preview()
        self.assertEqual(len(self.product.stock_valuation_layer_ids), before)

    def test_duplicate_serial_at_other_location_blocked(self):
        self.seed()
        location = self.location.copy({'name': 'Other Integrity Stock'})
        wizard = self.wizard()
        wizard.location_id = location
        with self.assertRaises(UserError):
            wizard.action_preview()

    def test_stale_preview_blocks_apply(self):
        self.seed()
        wizard = self.wizard(cost=120)
        wizard.action_preview()
        self.lot.standard_price = 110
        with self.assertRaises(UserError):
            wizard.action_apply()
        self.assertAlmostEqual(self.product.value_svl, 110)

    def test_failed_post_check_rolls_back_count_and_layers(self):
        from unittest.mock import patch
        before = len(self.product.stock_valuation_layer_ids)
        with self.assertRaises(UserError), self.cr.savepoint():
            with patch.object(type(self.env['stock.quant']), 'action_apply_inventory', return_value={'type': 'ir.actions.act_window'}):
                self.product._apply_counted_inventory(self.company, self.location, 1, lot=self.lot)
        self.env.invalidate_all()
        self.assertAlmostEqual(self.product.quantity_svl, 0)
        self.assertFalse(self.env['stock.quant'].search([
            ('product_id', '=', self.product.id), ('location_id', '=', self.location.id)]))
        self.assertEqual(len(self.product.stock_valuation_layer_ids), before)

    def test_zero_cached_serial_cost_blocks_legacy_removal(self):
        self.seed()
        self.lot.with_context(disable_auto_svl=True).write({'standard_price': 0})
        with self.assertRaises(UserError):
            self.product._apply_counted_inventory(self.company, self.location, 0, lot=self.lot)
        self.assertAlmostEqual(self.product.quantity_svl, 1)
        self.assertAlmostEqual(self.product.value_svl, 100)

    def test_reserved_serial_count_is_blocked(self):
        self.seed()
        self.env['stock.quant']._update_reserved_quantity(self.product, self.location, 1, lot_id=self.lot)
        with self.assertRaises(UserError):
            self.product._apply_counted_inventory(self.company, self.location, 0, lot=self.lot)
        self.assertAlmostEqual(self.product.quantity_svl, 1)

    def test_applied_excel_import_cannot_repeat(self):
        if 'import.stock.quant.wizard' not in self.env:
            self.skipTest('Excel import module is not installed')
        wizard = self.env['import.stock.quant.wizard'].create({'file': 'dGVzdA==', 'state': 'done'})
        with self.assertRaises(UserError):
            wizard.action_apply()
        self.assertAlmostEqual(self.product.quantity_svl, 0)

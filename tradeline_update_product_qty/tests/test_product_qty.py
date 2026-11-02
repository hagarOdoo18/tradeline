"""Exercise the existing Update Product QTY action with legacy valuation issues."""
from odoo import Command
from odoo.exceptions import UserError, ValidationError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestProductQtyWorkflow(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        accounts = cls.env['account.account'].create([
            {'name': name, 'code': code, 'account_type': kind,
             'company_ids': [Command.set(cls.company.ids)]}
            for name, code, kind in [
                ('Quantity Stock', 'QTY001', 'asset_current'),
                ('Quantity Difference', 'QTY002', 'expense'),
            ]
        ])
        journal = cls.env['account.journal'].create({
            'name': 'Quantity Adjustment Test', 'code': 'QTYT',
            'type': 'general', 'company_id': cls.company.id,
        })
        cls.category = cls.env['product.category'].create({
            'name': 'Quantity Adjustment AVCO', 'property_cost_method': 'average',
            'property_valuation': 'real_time',
            'property_stock_valuation_account_id': accounts[0].id,
            'property_stock_account_input_categ_id': accounts[1].id,
            'property_stock_account_output_categ_id': accounts[1].id,
            'property_stock_journal': journal.id,
        })
        cls.product = cls.env['product.product'].create({
            'name': 'Quantity Adjustment Item', 'is_storable': True,
            'categ_id': cls.category.id, 'standard_price': 100,
        })
        cls.location = cls.env['stock.location'].create({
            'name': 'Quantity Adjustment Stock', 'usage': 'internal',
            'company_id': cls.company.id,
        })

    def adjust(self, quantity, products=None, locations=None):
        wizard = self.env['update.product.qty.wizard'].create({
            'company_id': self.company.id, 'qty': quantity,
            'product_ids': [Command.set((products or self.product).ids)],
            'stock_ids': [Command.set((locations or self.location).ids)],
        })
        wizard.create_adjust()
        self.env.flush_all()
        self.env.invalidate_all()

    def add_legacy_layer(self, quantity, value):
        self.env['stock.valuation.layer'].create({
            'product_id': self.product.id, 'company_id': self.company.id,
            'quantity': quantity, 'value': value,
            'unit_cost': value / quantity if quantity else 0,
        })
        self.env.flush_all()
        self.env.invalidate_all()

    def assert_balances(self, physical, valued, value, product=None):
        product = product or self.product
        self.env.flush_all()
        self.env.invalidate_all()
        quants = self.env['stock.quant'].search([
            ('product_id', '=', product.id), ('company_id', '=', self.company.id),
            ('location_id.usage', '=', 'internal'),
        ])
        self.assertAlmostEqual(sum(quants.mapped('quantity')), physical)
        self.assertAlmostEqual(product.quantity_svl, valued)
        self.assertAlmostEqual(product.value_svl, value)
        posted_layers = product.stock_valuation_layer_ids.filtered(
            lambda layer: layer.stock_move_id and layer.value)
        self.assertTrue(all(layer.account_move_id.state == 'posted' for layer in posted_layers))

    def test_existing_gap_does_not_block_count_or_reconcile_history(self):
        self.adjust(3)
        self.add_legacy_layer(-2, -200)
        self.assert_balances(3, 1, 100)
        # Keep the strict repair-tool check; only the legacy form opts out.
        with self.assertRaises(UserError):
            self.product._check_inventory_valuation_integrity(self.company)
        self.adjust(4)
        self.assert_balances(4, 2, 200)
        self.assertFalse(self.product.stock_valuation_layer_ids.filtered('is_valuation_reconciliation'))
        self.assertTrue(self.env['stock.inventory.integrity.issue'].search([
            ('product_id', '=', self.product.id)]))
        audit = self.env['stock.count.cost.log'].search([
            ('product_id', '=', self.product.id)], order='id desc', limit=1)
        self.assertEqual((audit.previous_quantity, audit.counted_quantity, audit.quantity_difference), (3, 4, 1))
        self.assertEqual(sum(audit.valuation_layer_ids.mapped('quantity')), 1)

    def test_excess_valuation_does_not_block_removal(self):
        self.adjust(2)
        self.add_legacy_layer(3, 300)
        self.adjust(1)
        self.assert_balances(1, 4, 400)

    def test_zero_cost_with_existing_gap_posts_native_valuation(self):
        self.product.with_context(disable_auto_svl=True).standard_price = 0
        self.adjust(1)
        self.add_legacy_layer(2, 0)
        self.adjust(3)
        self.assert_balances(3, 5, 0)
        self.assertEqual(sum(self.product.stock_valuation_layer_ids.mapped('quantity')), 5)

    def test_cached_cost_mismatch_does_not_block_count(self):
        self.adjust(2)
        self.product.with_context(disable_auto_svl=True).standard_price = 120
        self.adjust(3)
        self.assert_balances(3, 3, 320)

    def test_unchanged_count_does_not_rewrite_valuation(self):
        self.adjust(2)
        self.add_legacy_layer(1, 100)
        before = self.product.stock_valuation_layer_ids
        self.adjust(2)
        self.assert_balances(2, 3, 300)
        self.assertEqual(self.product.stock_valuation_layer_ids, before)

    def test_multiple_products_and_locations_use_final_count(self):
        other_product = self.env['product.product'].create({
            'name': 'Other Quantity Item', 'is_storable': True,
            'categ_id': self.category.id, 'standard_price': 50,
        })
        other_location = self.location.copy({'name': 'Other Quantity Stock'})
        self.adjust(1)
        self.adjust(2, locations=other_location)
        self.adjust(4, products=other_product)
        self.adjust(1, products=other_product, locations=other_location)
        self.add_legacy_layer(4, 400)
        self.adjust(3, products=self.product | other_product, locations=self.location | other_location)
        self.assert_balances(6, 10, 1000)
        self.assert_balances(6, 6, 300, product=other_product)
        quants = self.env['stock.quant'].search([
            ('product_id', 'in', (self.product | other_product).ids),
            ('location_id', 'in', (self.location | other_location).ids),
        ])
        self.assertEqual(len(quants), 4)
        self.assertTrue(all(quant.quantity == 3 for quant in quants))

    def test_negative_count_still_rejected_without_posting(self):
        self.adjust(2)
        with self.assertRaises(ValidationError):
            self.adjust(-1)
        self.assert_balances(2, 2, 200)

    def test_serial_products_still_use_serial_count_workflow(self):
        self.product.tracking = 'serial'
        with self.assertRaises(ValidationError):
            self.adjust(1)
        self.assert_balances(0, 0, 0)

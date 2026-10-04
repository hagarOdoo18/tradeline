"""Integration tests of the actual multi-product workbook posting path."""
import base64
import io
from unittest import SkipTest
from openpyxl import Workbook
from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestMultiExcelFlow(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if "account.account" not in cls.env or "quantity_svl" not in cls.env["product.product"]._fields:
            raise SkipTest("Stock accounting is required for valuation assertions")
        cls.company = cls.env.company
        accounts = cls.env['account.account'].create([
            {'name': name, 'code': code, 'account_type': kind, 'company_ids': [Command.set(cls.company.ids)]}
            for name, code, kind in [('Excel Stock', 'XLS001', 'asset_current'), ('Excel Difference', 'XLS002', 'expense')]])
        journal = cls.env['account.journal'].create({'name': 'Excel Test', 'code': 'XLS', 'type': 'general', 'company_id': cls.company.id})
        category = cls.env['product.category'].create({
            'name': 'Excel AVCO', 'property_cost_method': 'average', 'property_valuation': 'real_time',
            'property_stock_valuation_account_id': accounts[0].id,
            'property_stock_account_input_categ_id': accounts[1].id,
            'property_stock_account_output_categ_id': accounts[1].id,
            'property_stock_journal': journal.id})
        cls.product = cls.env['product.product'].create({'name': 'Excel Test Item', 'default_code': 'EXCEL-AUDIT-001',
            'is_storable': True, 'categ_id': category.id, 'standard_price': 100})
        cls.location = cls.env['stock.location'].create({'name': 'Excel Audit Stock', 'usage': 'internal', 'company_id': cls.company.id})

    def excel(self, rows):
        order = self.env['stock.multi.update'].create({'location_id': self.location.id, 'notes': 'Test approved stock correction'})
        wb = Workbook()
        wb.active.append(['Product', 'Qty', 'Operation', 'Lot/Serial'])
        for row in rows:
            wb.active.append(row)
        stream = io.BytesIO()
        wb.save(stream)
        importer = self.env['stock.multi.update.import.wizard'].create({'update_id': order.id,
            'excel_file': base64.b64encode(stream.getvalue()), 'excel_filename': 'audit.xlsx'})
        importer.action_parse()
        self.assertEqual(importer.error_count, 0)
        importer.action_import()
        self.assertEqual(len(order.line_ids), len(rows))
        return order

    def balance(self, quantity, value, valuation_quantity=None):
        self.env.invalidate_all()
        quants = self.env['stock.quant'].search([('product_id', '=', self.product.id), ('location_id', '=', self.location.id)])
        self.assertAlmostEqual(sum(quants.mapped('quantity')), quantity)
        self.assertAlmostEqual(self.product.quantity_svl, quantity if valuation_quantity is None else valuation_quantity)
        self.assertAlmostEqual(self.product.value_svl, value)
        self.assertTrue(all(s.account_move_id.state == 'posted' for s in self.product.stock_valuation_layer_ids if s.value and s.stock_move_id))

    def test_excel_add_subtract_and_repeat_protection(self):
        self.excel([['EXCEL-AUDIT-001', 1, 'add', None]]).action_apply()
        order = self.excel([['EXCEL-AUDIT-001', 2, 'add', None]])
        order.action_apply()
        self.balance(3, 300)
        self.assertEqual((order.line_ids.qty_before, order.line_ids.qty_after), (1, 3))
        with self.assertRaises(UserError):
            order.action_apply()
        self.excel([['EXCEL-AUDIT-001', 1, 'subtract', None]]).action_apply()
        self.balance(2, 200)

    def test_excel_existing_gap_posts_both_deltas(self):
        self.excel([['EXCEL-AUDIT-001', 300, 'add', None]]).action_apply()
        # Reproduce the reported 300 physical / 2500 valued opening balance.
        self.env['stock.valuation.layer'].create({
            'product_id': self.product.id, 'company_id': self.company.id,
            'quantity': 2200, 'unit_cost': 100, 'value': 220000})
        self.env.invalidate_all()
        self.excel([['EXCEL-AUDIT-001', 2, 'add', None]]).action_apply()
        self.balance(302, 250200, valuation_quantity=2502)
        self.excel([['EXCEL-AUDIT-001', 1, 'subtract', None]]).action_apply()
        self.balance(301, 250100, valuation_quantity=2501)

    def test_excel_zero_cost_uses_native_valuation(self):
        self.product.with_context(disable_auto_svl=True).standard_price = 0
        self.excel([['EXCEL-AUDIT-001', 1, 'add', None]]).action_apply()
        self.balance(1, 0)

    def test_excel_cached_cost_mismatch_does_not_block(self):
        self.excel([['EXCEL-AUDIT-001', 1, 'add', None]]).action_apply()
        self.product.with_context(disable_auto_svl=True).standard_price = 120
        self.excel([['EXCEL-AUDIT-001', 1, 'add', None]]).action_apply()
        self.balance(2, 220)

    def test_excel_serial_add_and_remove(self):
        self.product.write({'tracking': 'serial', 'lot_valuated': True})
        self.env['stock.lot'].create({'name': 'EXCEL-SERIAL-001', 'product_id': self.product.id,
            'company_id': self.company.id, 'standard_price': 100})
        self.excel([['EXCEL-AUDIT-001', 1, 'add', 'EXCEL-SERIAL-001']]).action_apply()
        self.balance(1, 100)
        self.excel([['EXCEL-AUDIT-001', 1, 'subtract', 'EXCEL-SERIAL-001']]).action_apply()
        self.balance(0, 0)

    def test_excel_original_insufficient_stock_validation(self):
        self.excel([['EXCEL-AUDIT-001', 1, 'add', None]]).action_apply()
        order = self.excel([['EXCEL-AUDIT-001', 2, 'subtract', None]])
        with self.assertRaises(UserError), self.env.cr.savepoint():
            order.action_apply()
        self.balance(1, 100)

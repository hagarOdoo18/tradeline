"""Integration tests of the actual multi-product workbook posting path."""
import base64
import io
from openpyxl import Workbook
from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestMultiExcelFlow(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
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

    def balance(self, quantity, value):
        self.env.invalidate_all()
        quants = self.env['stock.quant'].search([('product_id', '=', self.product.id), ('location_id', '=', self.location.id)])
        self.assertAlmostEqual(sum(quants.mapped('quantity')), quantity)
        self.assertAlmostEqual(self.product.quantity_svl, quantity)
        self.assertAlmostEqual(self.product.value_svl, value)
        self.assertTrue(all(s.account_move_id.state == 'posted' for s in self.product.stock_valuation_layer_ids if s.value))

    def test_excel_add_subtract_and_repeat_protection(self):
        self.product._apply_counted_inventory(self.company, self.location, 1, reason='Approved opening')
        order = self.excel([['EXCEL-AUDIT-001', 2, 'add', None]])
        order.action_apply()
        self.balance(3, 300)
        self.assertEqual((order.line_ids.qty_before, order.line_ids.qty_after), (1, 3))
        with self.assertRaises(UserError):
            order.action_apply()
        self.excel([['EXCEL-AUDIT-001', 1, 'subtract', None]]).action_apply()
        self.balance(2, 200)

    def test_excel_existing_gap_blocked(self):
        self.product._apply_counted_inventory(self.company, self.location, 1, reason='Approved opening')
        self.env['stock.quant']._update_available_quantity(self.product, self.location, 2)
        order = self.excel([['EXCEL-AUDIT-001', 1, 'add', None]])
        with self.assertRaises(UserError), self.env.cr.savepoint():
            order.action_apply()
        self.env.invalidate_all()
        self.assertEqual(order.state, 'draft')
        self.assertAlmostEqual(self.product.quantity_svl, 1)
        self.assertAlmostEqual(sum(self.env['stock.quant'].search([('product_id', '=', self.product.id),
            ('location_id', '=', self.location.id)]).mapped('quantity')), 3)

    def test_excel_unknown_cost_blocked(self):
        self.product.with_context(disable_auto_svl=True).standard_price = 0
        order = self.excel([['EXCEL-AUDIT-001', 1, 'add', None]])
        with self.assertRaises(UserError), self.env.cr.savepoint():
            order.action_apply()
        self.balance(0, 0)

    def test_excel_serial_add_and_remove(self):
        self.product.write({'tracking': 'serial', 'lot_valuated': True})
        self.env['stock.lot'].create({'name': 'EXCEL-SERIAL-001', 'product_id': self.product.id,
            'company_id': self.company.id, 'standard_price': 100})
        self.excel([['EXCEL-AUDIT-001', 1, 'add', 'EXCEL-SERIAL-001']]).action_apply()
        self.balance(1, 100)
        self.excel([['EXCEL-AUDIT-001', 1, 'subtract', 'EXCEL-SERIAL-001']]).action_apply()
        self.balance(0, 0)

    def test_excel_duplicate_serial_blocked(self):
        self.product.write({'tracking': 'serial', 'lot_valuated': True})
        lot = self.env['stock.lot'].create({'name': 'EXCEL-SERIAL-001', 'product_id': self.product.id,
            'company_id': self.company.id, 'standard_price': 100})
        self.product._apply_counted_inventory(self.company, self.location, 1, lot=lot, reason='Approved opening')
        order = self.excel([['EXCEL-AUDIT-001', 1, 'add', 'EXCEL-SERIAL-001']])
        with self.assertRaises(UserError), self.env.cr.savepoint():
            order.action_apply()
        self.balance(1, 100)

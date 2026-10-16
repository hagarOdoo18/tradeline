"""Integration tests of the actual multi-product workbook posting path."""
import base64
import io
import logging
from time import perf_counter
from unittest import SkipTest
from unittest.mock import patch
from openpyxl import Workbook
from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

_logger = logging.getLogger(__name__)


class _RollbackReference(Exception):
    """Roll a reference execution back without retaining its stock changes."""


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

    def _document(self, rows):
        return self.env['stock.multi.update'].create({
            'location_id': self.location.id,
            'line_ids': [Command.create(dict(row, sequence=index))
                         for index, row in enumerate(rows)],
        })

    def _legacy_apply(self, document):
        """Frozen pre-batching posting path, for economic-result comparisons."""
        self.env.cr.execute(
            'SELECT id FROM stock_multi_update WHERE id = %s FOR UPDATE', (document.id,))
        document.invalidate_recordset()
        for line in document.line_ids:
            line._validate()
        for line in document.line_ids:
            lot = self.env['stock.lot']
            if line.tracking in ('serial', 'lot'):
                lot = line.lot_id
                if not lot and line.lot_name:
                    lot = self.env['stock.lot'].search([
                        ('name', '=', line.lot_name), ('product_id', '=', line.product_id.id),
                        ('company_id', 'in', [False, self.company.id]),
                    ], limit=1)
                    if not lot:
                        lot = self.env['stock.lot'].create({
                            'name': line.lot_name, 'product_id': line.product_id.id,
                            'company_id': self.company.id,
                        })
            quant = self.env['stock.quant'].search([
                ('product_id', '=', line.product_id.id), ('location_id', '=', line.location_id.id),
                ('company_id', '=', self.company.id), ('lot_id', '=', lot.id or False),
            ], limit=1)
            before = quant.quantity if quant else 0
            delta = line.qty if line.operation == 'add' else -line.qty
            if quant:
                quant.sudo().write({'inventory_quantity': before + delta})
            else:
                if delta < 0:
                    raise UserError('No existing stock to subtract from')
                quant = self.env['stock.quant'].sudo().with_company(self.company).with_context(
                    inventory_mode=True).create({
                        'product_id': line.product_id.id, 'location_id': line.location_id.id,
                        'lot_id': lot.id or False, 'inventory_quantity': delta,
                    })
            quant.sudo().with_context(
                inventory_name='Multi Stock Update %s' % document.name).action_apply_inventory()
            quant.invalidate_recordset()
            line.write({'qty_before': before, 'qty_after': quant.quantity})
        document.state = 'done'

    def _economic_snapshot(self, document, products):
        self.env.flush_all()
        products.invalidate_recordset()
        layers = self.env['stock.valuation.layer'].search([
            ('product_id', 'in', products.ids), ('company_id', '=', self.company.id),
        ], order='id')
        quants = self.env['stock.quant'].search([
            ('product_id', 'in', products.ids), ('location_id', '=', self.location.id),
        ])
        moves = self.env['stock.move'].search([
            ('product_id', 'in', products.ids), ('is_inventory', '=', True),
        ], order='id')
        journals = layers.account_move_id
        return {
            'state': document.state,
            'snapshots': [(l.qty_before, l.qty_after) for l in document.line_ids],
            'products': [(p.id, round(p.quantity_svl, 6), round(p.value_svl, 6),
                          round(p.standard_price, 6)) for p in products.sorted('id')],
            'physical': sorted((q.product_id.id, q.lot_id.name or '', round(q.quantity, 6))
                               for q in quants),
            'layers': sorted((s.product_id.id, s.lot_id.name or '', round(s.quantity, 6),
                              round(s.value, 6), round(s.unit_cost, 6),
                              round(s.remaining_qty, 6), round(s.remaining_value, 6))
                             for s in layers),
            'moves': sorted((m.product_id.id, m.location_id.id, m.location_dest_id.id,
                             round(m.quantity, 6), m.state) for m in moves),
            'ledger': sorted((l.product_id.id or 0, l.account_id.id,
                              round(l.debit, 6), round(l.credit, 6),
                              round(l.amount_currency, 6)) for l in journals.line_ids),
            'journal_states': sorted(journals.mapped('state')),
        }

    def _compare_with_legacy(self, rows, products, measure=False):
        metrics = {}
        try:
            with self.env.cr.savepoint():
                reference = self._document(rows)
                self.env.flush_all()
                self.env.invalidate_all()
                queries = self.env.cr.sql_log_count
                started = perf_counter()
                self._legacy_apply(reference)
                self.env.flush_all()
                metrics['legacy_seconds'] = perf_counter() - started
                metrics['legacy_queries'] = self.env.cr.sql_log_count - queries
                expected = self._economic_snapshot(reference, products)
                raise _RollbackReference()
        except _RollbackReference:
            pass
        actual = self._document(rows)
        self.env.flush_all()
        self.env.invalidate_all()
        queries = self.env.cr.sql_log_count
        started = perf_counter()
        actual.action_apply()
        self.env.flush_all()
        metrics['batch_seconds'] = perf_counter() - started
        metrics['batch_queries'] = self.env.cr.sql_log_count - queries
        self.assertEqual(self._economic_snapshot(actual, products), expected)
        if measure:
            _logger.info('Multi Stock Update 59-row benchmark: %s', metrics)
            self.assertLess(metrics['batch_queries'], metrics['legacy_queries'] * 0.85)
        return actual

    def test_batch_matches_sequential_avco_fifo_zero_cost_and_repeated_rows(self):
        Product = self.env['product.product']
        other = Product.create({'name': 'Batch Other', 'default_code': 'BATCH-OTHER',
                                'is_storable': True, 'categ_id': self.product.categ_id.id,
                                'standard_price': 25})
        zero = Product.create({'name': 'Batch Zero', 'default_code': 'BATCH-ZERO',
                               'is_storable': True, 'categ_id': self.product.categ_id.id,
                               'standard_price': 0})
        fifo_category = self.product.categ_id.copy({
            'name': 'Batch FIFO', 'property_cost_method': 'fifo'})
        fifo = Product.create({'name': 'Batch FIFO', 'default_code': 'BATCH-FIFO',
                               'is_storable': True, 'categ_id': fifo_category.id,
                               'standard_price': 60})
        products = self.product | other | zero | fifo
        opening = self._document([
            {'product_id': p.id, 'qty': 5, 'operation': 'add'}
            for p in self.product | other | fifo])
        self._legacy_apply(opening)
        self.product.with_context(disable_auto_svl=True).standard_price = 120
        rows = [
            {'product_id': self.product.id, 'qty': 2, 'operation': 'add'},
            {'product_id': other.id, 'qty': 1, 'operation': 'subtract'},
            {'product_id': self.product.id, 'qty': 1, 'operation': 'subtract'},
            {'product_id': zero.id, 'qty': 3, 'operation': 'add'},
            {'product_id': fifo.id, 'qty': 1, 'operation': 'subtract'},
            {'product_id': other.id, 'qty': 2, 'operation': 'add'},
            {'product_id': self.product.id, 'qty': 1, 'operation': 'add'},
            {'product_id': fifo.id, 'qty': 2, 'operation': 'add'},
        ]
        # A small cap also exercises a document with more independent products
        # than fit in one batch, as well as repeated rows for the same product.
        with patch.object(type(self.env['stock.multi.update']), '_posting_batch_size', 2):
            document = self._compare_with_legacy(rows, products)
        self.assertEqual([(l.qty_before, l.qty_after) for l in document.line_ids],
                         [(5, 7), (5, 4), (7, 6), (0, 3), (5, 4), (4, 6), (6, 7), (4, 6)])

    def test_batch_matches_sequential_serials_and_named_lots(self):
        self.product.write({'tracking': 'serial', 'lot_valuated': True})
        Product = self.env['product.product']
        other = Product.create({'name': 'Batch Serial', 'default_code': 'BATCH-SERIAL',
                                'is_storable': True, 'categ_id': self.product.categ_id.id,
                                'tracking': 'serial', 'lot_valuated': True, 'standard_price': 100})
        lot_product = Product.create({'name': 'Batch Lot', 'default_code': 'BATCH-LOT',
                                      'is_storable': True, 'categ_id': self.product.categ_id.id,
                                      'tracking': 'lot', 'lot_valuated': True, 'standard_price': 100})
        opening_lot = self.env['stock.lot'].create({
            'name': 'BATCH-OLD-SERIAL', 'product_id': self.product.id,
            'company_id': self.company.id, 'standard_price': 100})
        self._legacy_apply(self._document([{
            'product_id': self.product.id, 'lot_id': opening_lot.id,
            'qty': 1, 'operation': 'add',
        }]))
        self._compare_with_legacy([
            {'product_id': self.product.id, 'lot_name': 'BATCH-NEW-SERIAL',
             'qty': 1, 'operation': 'add'},
            {'product_id': other.id, 'lot_name': 'BATCH-OTHER-SERIAL',
             'qty': 1, 'operation': 'add'},
            {'product_id': self.product.id, 'lot_id': opening_lot.id,
             'qty': 1, 'operation': 'subtract'},
            {'product_id': lot_product.id, 'lot_name': 'BATCH-NEW-LOT',
             'qty': 3, 'operation': 'add'},
        ], self.product | other | lot_product)

    def test_batch_keeps_initial_subtraction_validation(self):
        document = self._document([
            {'product_id': self.product.id, 'qty': 2, 'operation': 'add'},
            {'product_id': self.product.id, 'qty': 1, 'operation': 'subtract'},
        ])
        # Existing behavior validates every subtraction against the starting
        # stock before posting; batching must not silently change that policy.
        with self.assertRaises(UserError), self.env.cr.savepoint():
            document.action_apply()
        self.balance(0, 0)
        self.assertEqual(document.state, 'draft')

    def test_later_serial_conflict_rolls_back_earlier_batch(self):
        self.product.write({'tracking': 'serial', 'lot_valuated': True})
        lot = self.env['stock.lot'].create({
            'name': 'BATCH-OCCUPIED', 'product_id': self.product.id,
            'company_id': self.company.id, 'standard_price': 100})
        self._legacy_apply(self._document([{
            'product_id': self.product.id, 'lot_id': lot.id,
            'qty': 1, 'operation': 'add',
        }]))
        document = self._document([
            {'product_id': self.product.id, 'lot_name': 'BATCH-ROLLBACK-NEW',
             'qty': 1, 'operation': 'add'},
            {'product_id': self.product.id, 'lot_id': lot.id,
             'qty': 1, 'operation': 'add'},
        ])
        with self.assertRaises(UserError), self.env.cr.savepoint():
            document.action_apply()
        self.balance(1, 100)
        self.assertEqual(document.state, 'draft')
        self.assertFalse(self.env['stock.lot'].search([
            ('product_id', '=', self.product.id), ('name', '=', 'BATCH-ROLLBACK-NEW')]))

    def test_batch_reduces_queries_without_changing_59_row_results(self):
        products = self.env['product.product'].create([{
            'name': 'Batch Benchmark %s' % n, 'is_storable': True,
            'categ_id': self.product.categ_id.id, 'standard_price': 10 + n,
        } for n in range(26)])
        self._legacy_apply(self._document([
            {'product_id': p.id, 'qty': 10, 'operation': 'add'} for p in products]))
        rows = [{'product_id': products[n % 26].id, 'qty': 1,
                 'operation': 'add' if n % 3 else 'subtract'} for n in range(59)]
        self._compare_with_legacy(rows, products, measure=True)

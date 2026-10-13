import base64
import io
import logging
from time import perf_counter
from unittest import SkipTest
from unittest.mock import patch

from openpyxl import Workbook
from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, new_test_user, tagged
from odoo.tools import mute_logger

_logger = logging.getLogger(__name__)


class StockImportTestCommon(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if 'quantity_svl' not in cls.env['product.product']._fields:
            raise SkipTest('Install stock_account to verify stock import valuation')
        cls.company = cls.env.company
        accounts = cls.env['account.account'].create([
            {'name': name, 'code': code, 'account_type': kind,
             'company_ids': [Command.set(cls.company.ids)]}
            for name, code, kind in [
                ('Import Stock', 'IMP001', 'asset_current'),
                ('Import Input', 'IMP002', 'asset_current'),
                ('Import Output', 'IMP003', 'expense'),
            ]
        ])
        journal = cls.env['account.journal'].create({
            'name': 'Excel Import Valuation', 'code': 'IMPT',
            'type': 'general', 'company_id': cls.company.id,
        })
        category = cls.env['product.category'].create({
            'name': 'Excel Import AVCO', 'property_cost_method': 'average', 'property_valuation': 'real_time',
            'property_stock_valuation_account_id': accounts[0].id,
            'property_stock_account_input_categ_id': accounts[1].id,
            'property_stock_account_output_categ_id': accounts[2].id,
            'property_stock_journal': journal.id,
        })
        cls.product = cls.env['product.product'].create({
            'name': 'Excel Batch Product', 'barcode': 'EXCEL-BATCH-' + cls.__name__,
            'is_storable': True, 'categ_id': category.id, 'standard_price': 100,
        })
        cls.location = cls.env['stock.location'].create({
            'name': 'Excel Batch Stock', 'usage': 'internal', 'company_id': cls.company.id,
        })

    def excel(self, rows):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(['Location', 'Item Code', 'Quantity', 'Serial'])
        for quantity, serial in rows:
            sheet.append([self.location.complete_name, self.product.barcode, quantity, serial])
        stream = io.BytesIO()
        workbook.save(stream)
        return self.env['import.stock.quant.wizard'].create({
            'file': base64.b64encode(stream.getvalue()), 'filename': 'batch-test.xlsx',
            'company_id': self.company.id,
        })

    def assert_balances(self, physical, valuation, value):
        self.env.flush_all()
        quants = self.env['stock.quant'].search([
            ('product_id', '=', self.product.id), ('location_id', '=', self.location.id),
        ])
        self.assertAlmostEqual(sum(quants.mapped('quantity')), physical)
        self.assertAlmostEqual(self.product.quantity_svl, valuation)
        self.assertAlmostEqual(self.product.value_svl, value)
        self.assertTrue(all(layer.account_move_id.state == 'posted'
                            for layer in self.product.stock_valuation_layer_ids if layer.value and layer.stock_move_id))

    def serial_batch(self, count):
        self.product.write({'tracking': 'serial', 'lot_valuated': True})
        wizard = self.excel([(1, 'EXCEL-BATCH-%05d' % number) for number in range(count)])
        started = perf_counter()
        wizard.action_preview()
        preview_time = perf_counter() - started
        self.assertEqual(len(wizard.line_ids), count)
        started = perf_counter()
        wizard.create_lots()
        lots_time = perf_counter() - started
        self.assertTrue(all(wizard.line_ids.mapped('is_valid')))
        self.assertEqual(len(wizard.line_ids.lot_id), count)
        wizard.line_ids.lot_id.with_context(disable_auto_svl=True).write({'standard_price': 100})
        started = perf_counter()
        wizard.action_apply()
        while wizard.state == 'processing':
            self.env['import.stock.quant.wizard']._cron_process_imports()
        apply_time = perf_counter() - started
        self.assertEqual(wizard.state, 'done')
        self.assert_balances(count, count, count * 100)
        _logger.info('Excel import benchmark %s rows: preview=%.2fs lots=%.2fs apply=%.2fs',
                     count, preview_time, lots_time, apply_time)


@tagged('post_install', '-at_install')
class TestStockImport(StockImportTestCommon):
    def test_100_serial_rows_post_stock_valuation_and_accounting(self):
        self.serial_batch(100)

    def test_repeated_product_rows_are_combined(self):
        wizard = self.excel([(2, None), (3, None), (-1, None)])
        wizard.action_preview()
        self.assertEqual(wizard.line_ids.mapped('projected_quantity'), [2, 5, 4])
        wizard.action_apply()
        self.assert_balances(4, 4, 400)
        with self.assertRaises(UserError):
            wizard.action_apply()
        self.assert_balances(4, 4, 400)

    def test_zero_cost_and_existing_gap_do_not_block_import(self):
        self.product.with_context(disable_auto_svl=True).standard_price = 0
        self.env['stock.quant']._update_available_quantity(self.product, self.location, 2)
        wizard = self.excel([(3, None)])
        wizard.action_preview()
        wizard.action_apply()
        self.assert_balances(5, 3, 0)

    def test_stale_preview_does_not_post_any_rows(self):
        wizard = self.excel([(3, None)])
        wizard.action_preview()
        self.env['stock.quant']._update_available_quantity(self.product, self.location, 2)
        with self.assertRaises(UserError):
            wizard.action_apply()
        self.assertEqual(wizard.state, 'preview')
        self.assert_balances(2, 0, 0)

    def test_same_serial_in_file_is_created_and_posted_once(self):
        self.product.write({'tracking': 'serial', 'lot_valuated': True})
        wizard = self.excel([(1, 'EXCEL-ONE-SERIAL'), (1, 'EXCEL-ONE-SERIAL')])
        wizard.action_preview()
        wizard.create_lots()
        self.assertEqual(len(wizard.line_ids.lot_id), 1)
        wizard.line_ids.lot_id.with_context(disable_auto_svl=True).standard_price = 100
        wizard.action_apply()
        self.assertEqual((wizard.applied_rows, wizard.skipped_rows), (1, 1))
        self.assert_balances(1, 1, 100)

    def test_serial_retry_skips_completed_stock_including_stale_preview(self):
        self.product.write({'tracking': 'serial', 'lot_valuated': True})
        first = self.excel([(1, 'EXCEL-RETRY-SERIAL')])
        first.action_preview()
        first.create_lots()
        stale = self.excel([(1, 'EXCEL-RETRY-SERIAL')])
        stale.action_preview()
        first.line_ids.lot_id.with_context(disable_auto_svl=True).standard_price = 100
        first.action_apply()
        stale.action_apply()
        self.assertEqual((stale.applied_rows, stale.skipped_rows), (0, 1))
        fresh = self.excel([(1, 'EXCEL-RETRY-SERIAL')])
        fresh.action_preview()
        self.assertTrue(fresh.line_ids.skipped)
        fresh.action_apply()
        self.assertEqual((fresh.applied_rows, fresh.skipped_rows), (0, 1))
        self.assert_balances(1, 1, 100)

    def test_serial_at_another_location_is_skipped_without_moving_stock(self):
        self.product.write({'tracking': 'serial', 'lot_valuated': True})
        first = self.excel([(1, 'EXCEL-OTHER-LOCATION')])
        first.action_preview()
        first.create_lots()
        first.action_apply()
        other = self.env['stock.location'].create({
            'name': 'Excel Other Stock', 'usage': 'internal', 'company_id': self.company.id,
        })
        retry = self.excel([(1, 'EXCEL-OTHER-LOCATION')])
        retry.action_preview()
        retry.line_ids.write({'location_id': other.id, 'location_name': other.complete_name,
                              'previous_quantity': 0})
        retry.action_apply()
        self.assertEqual(retry.skipped_rows, 1)
        self.assertIn('internal transfer', retry.line_ids.result_note)
        self.assertFalse(self.env['stock.quant'].search([
            ('product_id', '=', self.product.id), ('location_id', '=', other.id),
        ]))
        self.assert_balances(1, 1, 100)

    def test_skipped_serial_is_not_readded_if_stock_was_removed_after_preview(self):
        self.product.write({'tracking': 'serial', 'lot_valuated': True})
        first = self.excel([(1, 'EXCEL-SOLD-SERIAL')])
        first.action_preview()
        first.create_lots()
        first.action_apply()
        retry = self.excel([(1, 'EXCEL-SOLD-SERIAL')])
        retry.action_preview()
        self.assertTrue(retry.line_ids.skipped)
        removal = self.excel([(-1, 'EXCEL-SOLD-SERIAL')])
        removal.action_preview()
        removal.action_apply()
        with self.assertRaises(UserError):
            retry.action_apply()
        self.assert_balances(0, 0, 0)

    def test_background_batches_update_stock_and_accounting_and_do_not_repeat(self):
        self.product.write({'tracking': 'serial', 'lot_valuated': True})
        wizard = self.excel([(1, 'EXCEL-QUEUED-%04d' % number) for number in range(201)])
        wizard.action_preview()
        wizard.create_lots()
        wizard.line_ids.lot_id.with_context(disable_auto_svl=True).standard_price = 100
        wizard.action_apply()
        self.assertEqual(wizard.state, 'processing')
        self.assert_balances(0, 0, 0)
        for expected in (100, 200, 201):
            self.env['import.stock.quant.wizard']._cron_process_imports()
            self.assertEqual(wizard.applied_rows, expected)
            self.assertEqual(wizard.remaining_rows, 201 - expected)
            self.assert_balances(expected, expected, expected * 100)
        self.assertEqual(wizard.state, 'done')
        self.env['import.stock.quant.wizard']._cron_process_imports()
        with self.assertRaises(UserError):
            wizard.action_apply()
        self.assert_balances(201, 201, 20100)

    def test_background_combines_repeated_rows_in_one_batch(self):
        wizard = self.excel([(1, None)] * 201)
        wizard.action_preview()
        wizard.action_apply()
        self.env['import.stock.quant.wizard']._cron_process_imports()
        self.assertEqual((wizard.state, wizard.applied_rows), ('done', 201))
        self.assert_balances(201, 201, 20100)

    def test_background_uses_importing_staff_permissions_and_keeps_history_private(self):
        importer = new_test_user(self.env, login='excel_import_staff',
                                 groups='stock.group_stock_user,import_stock_quant_excel.group_import_stock_quant')
        colleague = new_test_user(self.env, login='excel_import_colleague',
                                  groups='stock.group_stock_user,import_stock_quant_excel.group_import_stock_quant')
        template = self.excel([(1, None)] * 101)
        wizard = self.env['import.stock.quant.wizard'].with_user(importer).create({
            'file': template.file, 'filename': 'staff-test.xlsx', 'company_id': self.company.id,
        })
        wizard.action_preview()
        wizard.action_apply()
        self.assertFalse(self.env['import.stock.quant.wizard'].with_user(colleague).search([('id', '=', wizard.id)]))
        self.assertFalse(self.env['import.stock.quant.line'].with_user(colleague).search([('wizard_id', '=', wizard.id)]))
        cron = self.env.ref('import_stock_quant_excel.ir_cron_process_stock_import')
        cron.method_direct_trigger()
        self.assertEqual((wizard.state, wizard.applied_rows), ('done', 101))
        self.assert_balances(101, 101, 10100)

    @mute_logger('odoo.addons.import_stock_quant_excel.models.import_stock_quant_wizard')
    def test_failed_background_batch_rolls_back_and_retry_keeps_completed_rows(self):
        self.product.with_context(disable_auto_svl=True).standard_price = 0
        self.product.write({'tracking': 'serial', 'lot_valuated': True})
        wizard = self.excel([(1, 'EXCEL-PAUSED-%04d' % number) for number in range(101)])
        wizard.action_preview()
        wizard.create_lots()
        wizard.action_apply()
        self.env['import.stock.quant.wizard']._cron_process_imports()
        self.assert_balances(100, 100, 0)
        original = type(wizard)._apply_lines

        def fail_after_posting(record, lines):
            original(record, lines)
            raise UserError('Test failure after native stock posting')

        with patch.object(type(wizard), '_apply_lines', fail_after_posting):
            self.env['import.stock.quant.wizard']._cron_process_imports()
        self.assertEqual(wizard.state, 'failed')
        self.assertEqual(wizard.remaining_rows, 1)
        self.assert_balances(100, 100, 0)
        wizard.action_retry()
        self.env['import.stock.quant.wizard']._cron_process_imports()
        self.assertEqual((wizard.state, wizard.applied_rows), ('done', 101))
        self.assert_balances(101, 101, 0)


@tagged('post_install', '-at_install', 'stock_import_large', '-standard')
class TestLargeStockImport(StockImportTestCommon):
    """Run explicitly on a test database with --test-tags=stock_import_large."""

    def test_500_serial_rows(self):
        self.serial_batch(500)

    def test_5000_serial_rows(self):
        self.serial_batch(5000)

    def test_5000_zero_cost_serial_rows_run_in_saved_batches(self):
        self.product.with_context(disable_auto_svl=True).standard_price = 0
        self.product.write({'tracking': 'serial', 'lot_valuated': True})
        wizard = self.excel([(1, 'EXCEL-ZERO-%05d' % number) for number in range(5000)])
        wizard.action_preview()
        wizard.create_lots()
        wizard.action_apply()
        self.assertEqual(wizard.state, 'processing')
        batches = 0
        while wizard.state == 'processing':
            self.env['import.stock.quant.wizard']._cron_process_imports()
            batches += 1
        self.assertEqual(batches, 50)
        self.assertEqual((wizard.state, wizard.applied_rows, wizard.remaining_rows), ('done', 5000, 0))
        self.assert_balances(5000, 5000, 0)

"""Run with odoo-bin shell on neutralized staging; never commits business data.

Save the three deployed sources as pos_before.py/invoice_before.py/apple_before.py
under ~/data/tl-pos-excel-perf-20261021 before overlaying the candidate files.
The original methods are compiled independently for output comparisons.
"""
import ast
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import time
from unittest.mock import patch
import zipfile

import openpyxl
import xlsxwriter
from odoo import fields, _
from odoo.exceptions import UserError


assert os.environ.get('ODOO_STAGE') == 'staging', 'STAGING ONLY'
assert env.cr.dbname == os.environ['PGDATABASE']
assert env['ir.config_parameter'].sudo().get_param('database.is_neutralized') == 'true'
root = Path.home() / 'data/tl-pos-excel-perf-20261021'
source_root = Path('/home/odoo/src/user')
results = []


def original_method(filename, class_name, method_name):
    tree = ast.parse((root / filename).read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == method_name)
    method.decorator_list = []
    namespace = dict(fields=fields, _=_, UserError=UserError, base64=base64,
                     io=io, BytesIO=io.BytesIO, xlsxwriter=xlsxwriter)
    exec(compile(ast.Module(body=[method], type_ignores=[]), filename, 'exec'), namespace)
    return namespace[method_name]


def signature(blob):
    # Compare decoded cells, formulas, sheet order and all style definitions.
    # XLSX ZIP bytes differ legitimately between shared and inline strings.
    digest = hashlib.sha256()
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        digest.update(archive.read('xl/styles.xml'))
    book = openpyxl.load_workbook(io.BytesIO(blob), read_only=True, data_only=False)
    counts = []
    for sheet in book:
        digest.update(sheet.title.encode())
        count = 0
        for row in sheet:
            cells = [(cell.value, cell.data_type, cell.number_format) for cell in row]
            digest.update(json.dumps(cells, default=str, ensure_ascii=False).encode())
            count += 1
        counts.append((sheet.title, count, sheet.max_column))
    book.close()
    return digest.hexdigest(), counts


def timed(label, callback):
    env.invalidate_all()
    queries = env.cr.sql_log_count
    started = time.perf_counter()
    value = callback()
    metric = dict(label=label, seconds=round(time.perf_counter() - started, 3),
                  queries=env.cr.sql_log_count - queries)
    print('POS_EXCEL_METRIC ' + json.dumps(metric), flush=True)
    results.append(metric)
    return value


def check_lots():
    Lot = env['stock.lot']
    names = Lot._load_pos_data_fields(0)
    # A representative bounded comparison first; no cashier session is loaded.
    ids = Lot.search([], limit=20000).ids
    domain = [('id', 'in', ids)]
    expected = timed('lot_native_20000', lambda: Lot.search_read(domain, names, load=False))
    with patch.object(type(Lot), '_load_pos_data_domain', return_value=domain):
        actual = timed('lot_optimized_20000', lambda: Lot._load_pos_data(
            {'pos.config': {'data': [{'id': 0}]}}))
    assert actual['fields'] == names
    assert len(actual['data']) == len(expected)
    for old, new in zip(expected, actual['data']):
        assert old.keys() == new.keys()
        for name in names:
            if name == 'product_qty':
                assert abs(old[name] - new[name]) <= 1e-8, 'Quantity mismatch'
            else:
                assert old[name] == new[name], 'Lot data/order mismatch'
    print('LOT_PAYLOAD_EQUIVALENT rows=%s' % len(expected), flush=True)


def check_invoices():
    old_action = original_method('invoice_before.py', 'ExportInvoiceMoveLineWizard', 'action_export_excel')
    Wizard = env['export.invoice.move.line.wizard']
    env.cr.execute("SELECT max(invoice_date) FROM account_move_line WHERE invoice_date IS NOT NULL")
    latest = env.cr.fetchone()[0]
    assert latest, 'No invoice dates for comparison'
    from datetime import timedelta
    branches = env['res.branch'].search([('company_id', '=', env.company.id)])
    roles = ['admin', 'manager', 'accounting', 'standard']
    real_has_group = type(env.user).has_group
    role_prefix = 'export_invoice_move_line.group_export_invoice_move_line_'
    for role in roles:
        def has_group(user, xmlid):
            if xmlid.startswith(role_prefix):
                return xmlid == role_prefix + role
            return real_has_group(user, xmlid)
        # Only select report-column variants; ACLs retain the current operator.
        with patch.object(type(env.user), 'has_group', has_group):
            wizard = Wizard.create({'branch_ids': [(6, 0, branches.ids)],
                                    'date_from': latest - timedelta(days=2), 'date_to': latest})
            timed('invoice_%s_native' % role, lambda: old_action(wizard))
            old = signature(base64.b64decode(wizard.file_data))
            timed('invoice_%s_optimized' % role, wizard.action_export_excel)
            new = signature(base64.b64decode(wizard.file_data))
            assert old == new, 'Excel cell/style mismatch for ' + role
            print('INVOICE_EQUIVALENT role=%s sheets=%s' % (role, new[1]), flush=True)


def check_apple():
    old_action = original_method('apple_before.py', 'AppleStockReportWizard', 'generate_xlsx_report')
    real_has_group = type(env.user).has_group
    for admin in (False, True):
        def has_group(user, xmlid):
            if xmlid == 'apple_stock_report_excel.group_export_applestock_vendor':
                return admin
            return real_has_group(user, xmlid)
        with patch.object(type(env.user), 'has_group', has_group):
            locations = env['stock.location'].search([
                ('usage', '=', 'internal'), ('company_id', '=', env.company.id)], limit=2)
            assert locations
            wizard = env['apple.stock.report.wizard'].create({'location_ids': [(6, 0, locations.ids)]})
            timed('apple_%s_native' % admin, lambda: old_action(wizard))
            old = signature(base64.b64decode(wizard.gentextfile))
            timed('apple_%s_optimized' % admin, wizard.generate_xlsx_report)
            new = signature(base64.b64decode(wizard.gentextfile))
            assert old == new, 'Apple Excel cell/style mismatch'
            print('APPLE_EQUIVALENT vendor_column=%s sheets=%s' % (admin, new[1]), flush=True)


try:
    # Never run payment providers or external network calls during comparisons.
    import requests
    with patch.object(requests.sessions.Session, 'request', side_effect=AssertionError('External HTTP disabled in QA')):
        check_lots()
        check_invoices()
        check_apple()
    (root / 'comparison-results.json').write_text(json.dumps(results, indent=2))
    print('POS_EXCEL_ALL_COMPARISONS_PASSED', flush=True)
finally:
    env.cr.rollback()
    print('ALL_COMPARISON_WRITES_ROLLED_BACK', flush=True)

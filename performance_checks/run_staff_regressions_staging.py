"""Rollback-only regression of the performance-only staging candidate."""
import json
import os
from pathlib import Path
from unittest.mock import patch
from odoo.tests.loader import get_module_test_cases, get_test_modules, run_suite
from odoo.tests.suite import OdooSuite

assert os.environ.get("ODOO_STAGE") == "staging", "STAGING ONLY"
assert env.cr.dbname == "tradelinestores-stage-40209081"
assert env["ir.config_parameter"].sudo().get_param("database.is_neutralized") == "true"
assert not env["import.stock.quant.wizard"].search_count([("state", "=", "processing")]), "An import is already running"
env.cr.rollback()
root = Path.home() / "data/tl-pos-excel-separated-20261023"
root.mkdir(parents=True, exist_ok=True)
modules = ["pos_product_by_lot_number", "pos_serial_validation", "import_stock_quant_excel", "stock_multi_update"]
excluded = {"TestLargeStockImport", "TestExcelImportPerformance"}
results = []
def refuse_http(*args, **kwargs):
    raise AssertionError("External HTTP disabled during staging QA")

try:
    with patch("requests.sessions.Session.request", side_effect=refuse_http):
        for module in modules:
            cases = [case for test_module in get_test_modules(module)
                     for case in get_module_test_cases(test_module)
                     if case.__class__.__name__ not in excluded]
            assert cases, "No regression cases found for " + module
            result = run_suite(OdooSuite(cases))
            item = dict(module=module, tests=result.testsRun, success=result.wasSuccessful(),
                        failures=result.failures_count, errors=result.errors_count)
            results.append(item)
            print("STAFF_REGRESSION " + json.dumps(item), flush=True)
            env.cr.rollback()
            (root / "staff-results.json").write_text(json.dumps(results, indent=2))
    assert all(item["success"] for item in results), "A staff workflow regression failed"
    print("STAFF_WORKFLOWS_PASSED", flush=True)
finally:
    env.cr.rollback()
    print("STAFF_QA_WRITES_ROLLED_BACK", flush=True)

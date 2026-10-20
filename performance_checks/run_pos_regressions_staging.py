"""Run TransactionCase suites on neutralized staging without a module upgrade."""
import json
import os
from unittest.mock import patch

from odoo.tests.loader import get_module_test_cases, get_test_modules, run_suite
from odoo.tests.suite import OdooSuite

assert os.environ.get('ODOO_STAGE') == 'staging', 'STAGING ONLY'
assert env.cr.dbname == os.environ['PGDATABASE']
assert env['ir.config_parameter'].sudo().get_param('database.is_neutralized') == 'true'
env.cr.rollback()
cases = [
    case
    for module in ['pos_product_by_lot_number', 'pos_serial_validation']
    for test_module in get_test_modules(module)
    for case in get_module_test_cases(test_module)
]
assert len(cases) >= 28, 'The complete expected suite must be present'
try:
    with patch('requests.sessions.Session.request', side_effect=AssertionError('External HTTP disabled in QA')):
        result = run_suite(OdooSuite(cases))
    print('STAGING_POS_REGRESSIONS ' + json.dumps({
        'tests': result.testsRun, 'success': result.wasSuccessful(),
        'failures': result.failures_count, 'errors': result.errors_count,
    }), flush=True)
    assert result.wasSuccessful(), 'Staging POS regression suite failed'
finally:
    env.cr.rollback()

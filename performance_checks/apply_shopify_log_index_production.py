"""Explicitly authorized production index install; no Odoo module upgrade.

Run with system Python in the identified production shell, outside a transaction.
The non-unique concurrent build permits ordinary log writes. Timeouts belong to
this maintenance connection only and do not throttle staff or integration calls.
"""
import os
import subprocess

DATABASE = 'tradelinestores-production-25284095'
assert os.environ.get('ODOO_STAGE') == 'production', 'PRODUCTION GUARD'
assert os.environ.get('PGDATABASE') == DATABASE, 'Unexpected database'


def sql(query):
    return subprocess.check_output(
        ['psql', '-X', '-A', '-t', '-v', 'ON_ERROR_STOP=1', '-c', query],
        text=True).strip()


assert sql('SELECT current_database()') == DATABASE
index = 'public.log_message__shopify_instance_id_index'
existing = sql("SELECT to_regclass('%s')" % index)
if existing:
    assert sql("SELECT indisvalid AND indisready AND NOT indisunique FROM pg_index WHERE indexrelid='%s'::regclass" % index) == 't', 'Inspect the existing index before retrying'
    assert '(shopify_instance_id)' in sql("SELECT pg_get_indexdef('%s'::regclass)" % index)
    print('PRODUCTION_LOG_INDEX_ALREADY_VALID', flush=True)
else:
    subprocess.run([
        'psql', '-X', '-v', 'ON_ERROR_STOP=1',
        # Concurrent builds may wait for an already-running transaction or
        # autovacuum. This matches the site's existing online-index procedure.
        '-c', "SET lock_timeout='5min'",
        '-c', "SET statement_timeout='600s'",
        '-c', 'CREATE INDEX CONCURRENTLY log_message__shopify_instance_id_index ON public.log_message (shopify_instance_id)',
    ], check=True)
    assert sql("SELECT indisvalid AND indisready AND NOT indisunique FROM pg_index WHERE indexrelid='%s'::regclass" % index) == 't'
    print('PRODUCTION_LOG_INDEX_CREATED_VALID', flush=True)

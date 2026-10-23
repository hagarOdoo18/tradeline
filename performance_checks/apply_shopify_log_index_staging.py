"""Install the ORM-declared log index without upgrading/rebuilding staging.

Run with system Python in the staging shell, outside an Odoo transaction.
Concurrent index creation allows existing log inserts/updates to continue.
This script deliberately refuses every database except the neutralized QA DB.
"""
import os
import subprocess

assert os.environ.get('ODOO_STAGE') == 'staging', 'STAGING ONLY'
assert os.environ.get('PGDATABASE') == 'tradelinestores-stage-40209081', 'Unexpected database'


def sql(query):
    return subprocess.check_output(
        ['psql', '-X', '-A', '-t', '-v', 'ON_ERROR_STOP=1', '-c', query],
        text=True).strip()


assert sql('SELECT current_database()') == 'tradelinestores-stage-40209081'
assert sql("SELECT value FROM ir_config_parameter WHERE key='database.is_neutralized'") == 'true'
subprocess.run([
    'psql', '-X', '-v', 'ON_ERROR_STOP=1',
    '-c', "SET lock_timeout='5s'",
    '-c', "SET statement_timeout='600s'",
    '-c', 'CREATE INDEX CONCURRENTLY IF NOT EXISTS log_message__shopify_instance_id_index ON public.log_message (shopify_instance_id)',
], check=True)
assert sql("SELECT indisvalid AND indisready FROM pg_index WHERE indexrelid='public.log_message__shopify_instance_id_index'::regclass") == 't'
assert '(shopify_instance_id)' in sql("SELECT pg_get_indexdef('public.log_message__shopify_instance_id_index'::regclass)")
print('STAGING_LOG_INDEX_VALID', flush=True)

"""Read-only staging log plans, exact counters and action payload checks."""
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

assert os.environ.get('ODOO_STAGE') == 'staging'
assert env.cr.dbname == 'tradelinestores-stage-40209081'
assert env['ir.config_parameter'].sudo().get_param('database.is_neutralized') == 'true'
env.cr.rollback()
env.cr.execute('SET TRANSACTION READ ONLY')
env.cr.execute("SET LOCAL statement_timeout='20s'")
root = Path.home() / 'data/tl-shopify-query-20261023'
root.mkdir(parents=True, exist_ok=True)
result = {}
for label, query in [
    ('count', 'SELECT count(*) FROM log_message WHERE shopify_instance_id=1'),
    ('first_page', 'SELECT id FROM log_message WHERE shopify_instance_id=1 ORDER BY id LIMIT 80'),
]:
    env.cr.execute('EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) ' + query)
    result[label] = env.cr.fetchone()[0]
env.cr.execute("SELECT pg_relation_size('public.log_message__shopify_instance_id_index'),indisvalid,indisready FROM pg_index WHERE indexrelid='public.log_message__shopify_instance_id_index'::regclass")
result['index_bytes'], result['index_valid'], result['index_ready'] = env.cr.fetchone()
model = env['shopify.configuration'].with_user(env.ref('base.user_admin'))
instances = model.search([])
field = model._fields['log_message_ids']
original_get = type(field).__get__


def no_expansion(descriptor, records, owner=None):
    assert descriptor is not field or records is None, 'Full log relation was loaded'
    return original_get(descriptor, records, owner)


with patch('requests.sessions.Session.request', side_effect=AssertionError('External HTTP disabled')), patch.object(type(field), '__get__', no_expansion):
    start = time.perf_counter()
    instances.read(['customer_count', 'product_count', 'order_count',
                    'log_message_count', 'collection_count', 'gift_card_count'])
    result['all_counters_seconds'] = time.perf_counter() - start
    result['instances'] = []
    for instance in instances:
        start = time.perf_counter()
        action = instance.shopify_log_message()
        seconds = time.perf_counter() - start
        expected = env['log.message'].with_user(model.env.user).search_count([('shopify_instance_id', '=', instance.id)])
        assert instance.log_message_count == expected
        item = dict(id=instance.id, exact_log_count=expected, action_seconds=seconds,
                    action_bytes=len(json.dumps(action)), no_log_id_expansion=True)
        if expected:
            assert action['domain'] == [('shopify_instance_id', '=', instance.id)]
            assert 'limit' not in action
            logs = env['log.message'].with_user(model.env.user)
            # Native paging only; the action itself retains the full dataset.
            first = logs.search(action['domain'], limit=80)
            second = logs.search(action['domain'], offset=80, limit=80)
            assert not (first & second)
            item.update(first_page=len(first), second_page=len(second))
        result['instances'].append(item)
result['source_hashes'] = {}
import subprocess
for name in ['shopify_odoo_connector/models/shopify_configuration.py', 'shopify_odoo_connector/models/log_message.py']:
    result['source_hashes'][name] = subprocess.check_output(['git','hash-object',name],cwd='/home/odoo/src/user',text=True).strip()
(root / 'benchmark.json').write_text(json.dumps(result, indent=2))
print('SHOPIFY_BENCHMARK', json.dumps({k: v for k, v in result.items() if k not in ('count','first_page','source_hashes')}), flush=True)
for label in ('count','first_page'):
    plan = result[label][0]
    print('PLAN', label, plan['Execution Time'], 'ms', 'read_blocks', plan['Plan'].get('Shared Read Blocks'), flush=True)
env.cr.rollback()
print('READ_ONLY_BENCHMARK_COMPLETE', flush=True)

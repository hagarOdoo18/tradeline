"""Post-release checks; read-only transaction, no test fixtures or business actions."""
import json
import os
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

assert os.environ.get('ODOO_STAGE') == 'production'
assert env.cr.dbname == 'tradelinestores-production-25284095'
env.cr.rollback()
env.cr.execute('SET TRANSACTION READ ONLY')
env.cr.execute("SET LOCAL statement_timeout='20s'")
repo = '/home/odoo/src/user'
result = {'database': env.cr.dbname,
          'head': subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip(),
          'reads': []}
with patch('requests.sessions.Session.request', side_effect=AssertionError('No external HTTP during release verification')):
    for model, names in [
        ('sale.order', ['id','state']), ('stock.picking', ['id','state']),
        ('account.move', ['id','state']), ('res.partner', ['id','active']),
        ('purchase.order', ['id','state']), ('pos.order', ['id','state']),
        ('stock.quant', ['id','quantity','reserved_quantity']),
    ]:
        start = time.perf_counter()
        rows = env[model].with_user(env.ref('base.user_admin')).search_read([],names,limit=5)
        result['reads'].append({'model':model,'rows':len(rows),'seconds':time.perf_counter()-start})
    instances = env['shopify.configuration'].with_user(env.ref('base.user_admin')).search([])
    start = time.perf_counter()
    instances.read(['customer_count','product_count','order_count','log_message_count','collection_count','gift_card_count'])
    result['shopify_counters_seconds'] = time.perf_counter()-start
    result['shopify'] = []
    for instance in instances:
        action = instance.shopify_log_message()
        count = env['log.message'].with_user(instances.env.user).search_count([('shopify_instance_id','=',instance.id)])
        assert instance.log_message_count == count
        if count:
            assert action['domain'] == [('shopify_instance_id','=',instance.id)]
            assert 'limit' not in action
        result['shopify'].append({'id':instance.id,'exact_log_count':count,'action_bytes':len(json.dumps(action))})
    # A bounded sample checks the deployed loader against native computed values.
    Lot = env['stock.lot'].with_user(env.ref('base.user_admin'))
    ids = Lot.search([],limit=100).ids
    domain = [('id','in',ids)]
    fields = Lot._load_pos_data_fields(0)
    expected = Lot.search_read(domain, fields, load=False)
    with patch.object(type(Lot),'_load_pos_data_domain',return_value=domain):
        actual = Lot._load_pos_data({'pos.config':{'data':[{'id':0}]}})
    assert actual['fields'] == fields
    assert len(actual['data']) == len(expected)
    for native, optimized in zip(expected,actual['data']):
        assert native.keys() == optimized.keys()
        for name in fields:
            if name == 'product_qty':
                assert abs(native[name]-optimized[name]) <= 1e-8
            else:
                assert native[name] == optimized[name]
    result['native_pos_lot_sample_matches'] = len(expected)
    env.cr.execute("SELECT indisvalid,indisready,pg_relation_size(indexrelid) FROM pg_index WHERE indexrelid='public.log_message__shopify_instance_id_index'::regclass")
    valid, ready, size = env.cr.fetchone()
    assert valid and ready
    result['log_index'] = {'valid':valid,'ready':ready,'bytes':size}
env.cr.rollback()
root = Path.home()/'data/tl-performance-release-20261023'
root.mkdir(parents=True,exist_ok=True)
(root/'production-readonly-results.json').write_text(json.dumps(result,indent=2))
print('PRODUCTION_READ_ONLY_CHECKS_PASSED', json.dumps(result), flush=True)

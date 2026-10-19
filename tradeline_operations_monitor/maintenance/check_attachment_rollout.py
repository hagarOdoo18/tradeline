"""Read-only rollout evidence. Run with the hosting shell's PostgreSQL defaults."""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
from psycopg2 import sql

TABLES = ('sale_order', 'sale_order_line', 'account_move', 'account_move_line',
          'account_payment', 'stock_move', 'stock_valuation_layer', 'stock_quant')
TARGETS = (('account_move', 'extract_attachment_id'),
           ('account_move', 'e_invoice_file'),
           ('account_move', 'l10n_eg_eta_json_doc_id'),
           ('account_move', 'message_main_attachment_id'),
           ('ir_attachment', 'original_id'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--save', required=True)
    parser.add_argument('--compare')
    parser.add_argument('--require-no-business-writes', action='store_true')
    parser.add_argument('--plans', action='store_true')
    args = parser.parse_args()
    before = json.loads(Path(args.compare).read_text()) if args.compare else None
    conn = psycopg2.connect('')
    conn.set_session(readonly=True, autocommit=True)
    result = {'at_utc': datetime.now(timezone.utc).isoformat(), 'tables': {}, 'indexes': []}
    with conn.cursor() as cr:
        cr.execute("SET statement_timeout='45s'")
        cr.execute("SET lock_timeout='1s'")
        cr.execute('SELECT current_database(), pg_postmaster_start_time()::text')
        result['database'], result['postmaster_started'] = cr.fetchone()
        for table in TABLES:
            cr.execute(sql.SQL('SELECT count(*) FROM {}').format(sql.Identifier(table)))
            row_count = cr.fetchone()[0]
            if before:
                ids = before['tables'][table]['sample_ids']
            else:
                cr.execute(sql.SQL('SELECT id FROM {} ORDER BY id LIMIT 1000').format(sql.Identifier(table)))
                ids = [row[0] for row in cr.fetchall()]
            # Hash on the server; neither customer details nor financial row
            # contents are written to the evidence files or terminal output.
            cr.execute(sql.SQL('''SELECT count(*), md5(COALESCE(string_agg(
                md5(row_to_json(t)::text), '' ORDER BY t.id), ''))
                FROM {} t WHERE id=ANY(%s)''').format(sql.Identifier(table)), [ids])
            sample_count, digest = cr.fetchone()
            cr.execute('SELECT pg_stat_clear_snapshot()')
            cr.execute('''SELECT n_tup_ins,n_tup_upd,n_tup_del FROM pg_stat_user_tables
                          WHERE relid=%s::regclass''', [table])
            writes = list(cr.fetchone())
            result['tables'][table] = dict(count=row_count, sample_ids=ids,
                                          sample_count=sample_count, digest=digest, writes=writes)
        for table, column in TARGETS:
            cr.execute('''SELECT ic.relname,i.indisvalid,i.indisready,pg_size_pretty(pg_relation_size(ic.oid))
                FROM pg_index i JOIN pg_class ic ON ic.oid=i.indexrelid
                JOIN pg_am am ON am.oid=ic.relam
                JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=i.indkey[0]
                WHERE i.indrelid=%s::regclass AND a.attname=%s
                  AND am.amname='btree'
                  AND (i.indpred IS NULL OR pg_get_expr(i.indpred,i.indrelid)=
                       '(' || quote_ident(a.attname) || ' IS NOT NULL)')
                ORDER BY ic.relname''', [table, column])
            indexes = cr.fetchall()
            entry = dict(table=table, column=column, indexes=indexes)
            if args.plans:
                cr.execute(sql.SQL('EXPLAIN (ANALYZE, BUFFERS, COSTS OFF) SELECT 1 FROM ONLY {} WHERE {}=%s')
                           .format(sql.Identifier(table), sql.Identifier(column)), [255952])
                entry['plan'] = [row[0] for row in cr.fetchall()]
            result['indexes'].append(entry)
    conn.close()
    Path(args.save).write_text(json.dumps(result, indent=2))
    print('Database:', result['database'])
    failed = False
    for table, item in result['tables'].items():
        if before:
            old = before['tables'][table]
            same = item['digest'] == old['digest'] and item['sample_count'] == old['sample_count']
            deltas = [n-o for n, o in zip(item['writes'], old['writes'])]
            print(table, 'sample unchanged:', same, 'row delta:', item['count']-old['count'], 'DML deltas:', deltas)
            failed |= not same
            if args.require_no_business_writes:
                failed |= item['count'] != old['count'] or any(deltas)
        else:
            print(table, 'rows:', item['count'], 'fingerprinted:', item['sample_count'])
    for entry in result['indexes']:
        valid = [row[0] for row in entry['indexes'] if row[1] and row[2]]
        print(entry['table'] + '.' + entry['column'], 'valid indexes:', valid)
        if args.plans:
            print('\n'.join(entry['plan']))
        if before:
            failed |= not valid
    if before and before['database'] != result['database']:
        raise RuntimeError('Before and after evidence belong to different databases')
    if failed:
        raise RuntimeError('Rollout verification needs investigation; see saved evidence')


if __name__ == '__main__':
    main()

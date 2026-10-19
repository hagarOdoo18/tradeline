-- Online attachment-reference index maintenance, prepared 19 October 2026.
-- Run with psql -X -v ON_ERROR_STOP=1 -f attachment-reference-indexes.sql
-- Validate on staging first, then use an agreed production maintenance window.
-- Do not wrap in BEGIN/COMMIT or pass --single-transaction: concurrent builds
-- require autocommit. Builds permit normal writes but add CPU/I/O load.
-- No attachment, invoice, stock, payment, or accounting values are changed.
-- If a build fails, inspect index validity before retrying; do not silently
-- retain an invalid index or drop an existing index without reviewing it.

\set ON_ERROR_STOP on
\timing on

SELECT current_database() = 'tradelinestores-production-25284095'
       OR current_database() LIKE 'tradelinestores-stage-%' AS intended_database
\gset
\if :intended_database
\else
  \echo 'Refusing to run outside the identified Tradeline production/staging databases.'
  \quit 1
\endif

-- Concurrent builds can wait for an older Odoo snapshot to finish. They do
-- not lock out ordinary sales/accounting writes while waiting or building.
SET lock_timeout = '5min';
SET statement_timeout = '30min';

-- Fail before any build if the expected attachment foreign keys are absent.
DO $$
DECLARE target record;
BEGIN
  FOR target IN SELECT * FROM (VALUES
      ('account_move', 'extract_attachment_id'),
      ('account_move', 'e_invoice_file'),
      ('account_move', 'l10n_eg_eta_json_doc_id'),
      ('account_move', 'message_main_attachment_id'),
      ('ir_attachment', 'original_id')
  ) AS t(table_name, column_name)
  LOOP
    IF NOT EXISTS (
      SELECT 1 FROM pg_constraint c
      JOIN pg_attribute a ON a.attrelid=c.conrelid AND c.conkey=ARRAY[a.attnum]
      WHERE c.contype='f' AND c.conrelid=to_regclass('public.' || target.table_name)
        AND c.confrelid='public.ir_attachment'::regclass
        AND a.attname=target.column_name
    ) THEN
      RAISE EXCEPTION 'Expected attachment FK missing: %.%', target.table_name, target.column_name;
    END IF;
  END LOOP;
END $$;

-- Preserve full indexes and Odoo's partial WHERE column IS NOT NULL indexes:
-- both support the non-null equality lookup performed by a foreign key.
-- Generate a build only for an actually missing reference index.
WITH targets(table_name, column_name) AS (VALUES
    ('account_move', 'extract_attachment_id'),
    ('account_move', 'e_invoice_file'),
    ('account_move', 'l10n_eg_eta_json_doc_id'),
    ('account_move', 'message_main_attachment_id'),
    ('ir_attachment', 'original_id')
)
SELECT format('CREATE INDEX CONCURRENTLY %I ON public.%I (%I)',
              'tl_fk_' || t.table_name || '_' || t.column_name,
              t.table_name, t.column_name)
FROM targets t
JOIN pg_attribute a ON a.attrelid=to_regclass('public.' || t.table_name)
                   AND a.attname=t.column_name AND NOT a.attisdropped
WHERE NOT EXISTS (
    SELECT 1 FROM pg_index i JOIN pg_class ic ON ic.oid=i.indexrelid
    JOIN pg_am am ON am.oid=ic.relam
    WHERE i.indrelid=a.attrelid AND i.indkey[0]=a.attnum
      AND i.indisvalid AND i.indisready AND am.amname='btree'
      AND (i.indpred IS NULL OR pg_get_expr(i.indpred,i.indrelid)=
           '(' || quote_ident(a.attname) || ' IS NOT NULL)')
)
ORDER BY t.table_name, t.column_name
\gexec

-- Every listed reference must have a usable leading-column B-tree afterward.
WITH targets(table_name, column_name) AS (VALUES
    ('account_move', 'extract_attachment_id'),
    ('account_move', 'e_invoice_file'),
    ('account_move', 'l10n_eg_eta_json_doc_id'),
    ('account_move', 'message_main_attachment_id'),
    ('ir_attachment', 'original_id')
)
SELECT bool_and(EXISTS (
    SELECT 1 FROM pg_index i JOIN pg_class ic ON ic.oid=i.indexrelid
    JOIN pg_am am ON am.oid=ic.relam
    WHERE i.indrelid=a.attrelid AND i.indkey[0]=a.attnum
      AND i.indisvalid AND i.indisready AND am.amname='btree'
      AND (i.indpred IS NULL OR pg_get_expr(i.indpred,i.indrelid)=
           '(' || quote_ident(a.attname) || ' IS NOT NULL)')
)) AS all_references_indexed
FROM targets t
JOIN pg_attribute a ON a.attrelid=to_regclass('public.' || t.table_name)
                   AND a.attname=t.column_name AND NOT a.attisdropped
\gset
\if :all_references_indexed
  \echo 'All five attachment references have valid indexes. Check plans and the next scheduled cleanup duration.'
\else
  \echo 'Verification failed: at least one expected usable index is missing.'
  \quit 1
\endif

RESET lock_timeout;
RESET statement_timeout;

-- ============================================================================
-- Monthly audit: Invoice Analysis  vs  Full-History "current" lines
-- ----------------------------------------------------------------------------
-- Source of truth: account_invoice_report (the "Invoices Analysis" report, as
-- overridden in accounting_customization). The Full-History model
-- (legacy.current.product.history) builds its "current" rows from the very same
-- report, but only for products it can bucket (those whose barcode/default_code
-- yields a non-empty 5-char normalized prefix) and groups them by that prefix.
--
-- This query makes the gap auditable, one row per month:
--   ia_*        = Invoice Analysis totals (the truth) for the comparison scope
--   bucketed_*  = the slice the module CAN bucket (non-empty code prefix)
--   nocode_*    = the residual the module DROPS (no usable code) -> structural
--   module_*    = what the Full-History "current" lines actually sum to
--   *_diff_*    = module minus bucketed; should be ~0. Non-zero => a real bug
--                 (double counting, sign error, etc.), not just the no-code gap.
--
-- By construction: ia_qty = bucketed_qty + nocode_qty (same for amount).
-- Comparison scope mirrors the module exactly:
--   move_type IN ('out_invoice','out_refund'), product set, invoice_date >= 2026-01-01.
-- ============================================================================

WITH ia AS (
    SELECT
        date_trunc('month', air.invoice_date)::date AS period_month,
        air.quantity      AS qty,
        air.price_subtotal AS amount,
        -- Same code resolution as the module: first usable of barcode, default_code.
        NULLIF(
            REGEXP_REPLACE(
                UPPER(COALESCE(
                    CASE
                        WHEN pp.barcode IS NULL THEN NULL
                        WHEN LOWER(BTRIM(COALESCE(pp.barcode, ''))) IN ('', 'false', 'none', 'null') THEN NULL
                        ELSE BTRIM(pp.barcode)
                    END,
                    CASE
                        WHEN pp.default_code IS NULL THEN NULL
                        WHEN LOWER(BTRIM(COALESCE(pp.default_code, ''))) IN ('', 'false', 'none', 'null') THEN NULL
                        ELSE BTRIM(pp.default_code)
                    END,
                    ''
                )),
                '[^A-Z0-9]+', '', 'g'
            ),
            ''
        ) AS norm_code
    FROM account_invoice_report air
    JOIN product_product pp
        ON pp.id = air.product_id
    WHERE air.product_id IS NOT NULL
      AND air.move_type IN ('out_invoice', 'out_refund')
      AND air.invoice_date >= DATE '2026-01-01'
),
ia_flagged AS (
    SELECT
        period_month,
        qty,
        amount,
        (NULLIF(LEFT(COALESCE(norm_code, ''), 5), '') IS NOT NULL) AS bucketable
    FROM ia
),
ia_agg AS (
    SELECT
        period_month,
        SUM(qty)                                          AS ia_qty,
        SUM(amount)                                       AS ia_amount,
        SUM(qty)    FILTER (WHERE bucketable)             AS bucketed_qty,
        SUM(amount) FILTER (WHERE bucketable)             AS bucketed_amount,
        COALESCE(SUM(qty)    FILTER (WHERE NOT bucketable), 0) AS nocode_qty,
        COALESCE(SUM(amount) FILTER (WHERE NOT bucketable), 0) AS nocode_amount
    FROM ia_flagged
    GROUP BY period_month
),
module_agg AS (
    SELECT
        period_month,
        SUM(sales_qty)    AS module_qty,
        SUM(sales_amount) AS module_amount
    FROM legacy_current_product_history
    WHERE source_system = 'current'
    GROUP BY period_month
)
SELECT
    to_char(COALESCE(i.period_month, m.period_month), 'YYYY-MM') AS month,
    ROUND(COALESCE(i.ia_qty, 0)::numeric, 3)            AS ia_qty,
    ROUND(COALESCE(i.ia_amount, 0)::numeric, 2)         AS ia_amount,
    ROUND(COALESCE(i.bucketed_qty, 0)::numeric, 3)      AS bucketed_qty,
    ROUND(COALESCE(i.bucketed_amount, 0)::numeric, 2)   AS bucketed_amount,
    ROUND(COALESCE(i.nocode_qty, 0)::numeric, 3)        AS nocode_qty,
    ROUND(COALESCE(i.nocode_amount, 0)::numeric, 2)     AS nocode_amount,
    ROUND(COALESCE(m.module_qty, 0)::numeric, 3)        AS module_qty,
    ROUND(COALESCE(m.module_amount, 0)::numeric, 2)     AS module_amount,
    ROUND((COALESCE(m.module_qty, 0) - COALESCE(i.bucketed_qty, 0))::numeric, 3)       AS qty_diff_module_vs_bucketed,
    ROUND((COALESCE(m.module_amount, 0) - COALESCE(i.bucketed_amount, 0))::numeric, 2) AS amount_diff_module_vs_bucketed
FROM ia_agg i
FULL OUTER JOIN module_agg m
    ON m.period_month = i.period_month
ORDER BY 1;

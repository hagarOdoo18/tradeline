from odoo import fields, models, tools


MATCH_STATUS_SELECTION = [
    ("matched", "Matched"),
    ("review", "Needs Review"),
    ("unmatched", "Unmatched"),
]

MATCH_METHOD_SELECTION = [
    ("auto_barcode", "Auto (Barcode)"),
    ("auto_code", "Auto (Item Code)"),
    ("manual", "Manual"),
    ("none", "None"),
]


class LegacyProductMonthFact(models.Model):
    _name = "legacy.product.month.fact"
    _description = "Legacy Product Monthly Fact"
    _order = "period_month desc, source_default_code, source_name, id"
    _rec_name = "source_name"

    source_db = fields.Char(required=True, index=True)
    source_product_id = fields.Integer(required=True, index=True)
    period_month = fields.Date(required=True, index=True)
    warehouse_key = fields.Char(default="all", required=True, index=True)

    source_default_code = fields.Char(index=True)
    source_barcode = fields.Char(index=True)
    source_name = fields.Char(index=True)
    source_category_name = fields.Char(index=True)
    source_brand_name = fields.Char(index=True)

    legacy_sales_qty = fields.Float()
    legacy_sales_amount = fields.Float()
    legacy_stock_close_qty = fields.Float()
    legacy_stock_close_value = fields.Float()
    value_available = fields.Boolean(default=False, index=True)

    import_batch_id = fields.Char(index=True)
    imported_at = fields.Datetime(default=fields.Datetime.now, index=True)
    legacy_payload = fields.Json()

    _sql_constraints = [
        (
            "legacy_product_month_fact_uniq",
            "unique(source_db, source_product_id, period_month, warehouse_key)",
            "Legacy monthly product fact identity must be unique.",
        ),
    ]


class LegacyCurrentProductCompareMonth(models.Model):
    _name = "legacy.current.product.compare.month"
    _description = "Legacy to Current Product Comparison (Monthly)"
    _auto = False
    _order = "period_month desc, source_default_code, source_name, id"
    _rec_name = "source_name"

    source_db = fields.Char(readonly=True)
    source_product_id = fields.Integer(readonly=True)
    period_month = fields.Date(readonly=True)
    warehouse_key = fields.Char(readonly=True)

    source_default_code = fields.Char(readonly=True)
    source_barcode = fields.Char(readonly=True)
    source_name = fields.Char(readonly=True)
    source_category_name = fields.Char(readonly=True)
    source_brand_name = fields.Char(readonly=True)

    target_product_id = fields.Many2one("product.product", readonly=True)
    target_product_tmpl_id = fields.Many2one("product.template", readonly=True)
    target_default_code = fields.Char(readonly=True)
    target_barcode = fields.Char(readonly=True)
    target_name = fields.Char(readonly=True)

    match_status = fields.Selection(selection=MATCH_STATUS_SELECTION, readonly=True)
    match_method = fields.Selection(selection=MATCH_METHOD_SELECTION, readonly=True)
    confidence = fields.Float(readonly=True)
    manual_override = fields.Boolean(readonly=True)

    legacy_sales_qty = fields.Float(readonly=True)
    legacy_sales_amount = fields.Float(readonly=True)
    legacy_stock_close_qty = fields.Float(readonly=True)
    legacy_stock_close_value = fields.Float(readonly=True)

    current_sales_qty = fields.Float(readonly=True)
    current_sales_amount = fields.Float(readonly=True)
    current_stock_close_qty = fields.Float(readonly=True)
    current_stock_close_value = fields.Float(readonly=True)

    delta_sales_qty = fields.Float(readonly=True)
    delta_sales_amount = fields.Float(readonly=True)
    delta_stock_value = fields.Float(readonly=True)
    value_available = fields.Boolean(readonly=True)

    has_legacy_data = fields.Boolean(readonly=True)
    has_current_data = fields.Boolean(readonly=True)

    def _table_exists(self, table_name):
        self.env.cr.execute(
            """
            SELECT EXISTS(
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = %s
            )
            """,
            (table_name,),
        )
        return bool(self.env.cr.fetchone()[0])

    def _has_svl_schema(self):
        if not self._table_exists("stock_valuation_layer"):
            return False
        self.env.cr.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'stock_valuation_layer'
              AND column_name IN ('product_id', 'quantity', 'value', 'create_date')
            """
        )
        cols = {row[0] for row in self.env.cr.fetchall()}
        return {"product_id", "quantity", "value", "create_date"}.issubset(cols)

    def action_open_mapping(self):
        self.ensure_one()
        domain = [
            ("source_db", "=", self.source_db or ""),
            ("source_product_id", "=", self.source_product_id or 0),
        ]
        mapping = self.env["legacy.product.map"].search(domain, limit=1)
        action = self.env["ir.actions.actions"]._for_xml_id("legacy_invoice_archive.action_legacy_product_map")
        if mapping:
            action.update(
                {
                    "res_id": mapping.id,
                    "view_mode": "form",
                    "views": [(False, "form")],
                    "target": "current",
                }
            )
        else:
            action["domain"] = domain
        return action

    def action_open_target_product(self):
        self.ensure_one()
        if not self.target_product_id:
            return False
        return {
            "type": "ir.actions.act_window",
            "name": "Product",
            "res_model": "product.product",
            "res_id": self.target_product_id.id,
            "view_mode": "form",
            "target": "current",
        }

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)

        svl_available = self._has_svl_schema()
        if svl_available:
            current_stock_cte = """
            svl_open AS (
                SELECT
                    svl.product_id,
                    SUM(COALESCE(svl.quantity, 0.0)) AS qty_open,
                    SUM(COALESCE(svl.value, 0.0)) AS value_open
                FROM stock_valuation_layer svl
                WHERE svl.create_date < DATE '2026-01-01'
                GROUP BY svl.product_id
            ),
            svl_month_delta AS (
                SELECT
                    svl.product_id,
                    date_trunc('month', svl.create_date)::date AS period_month,
                    SUM(COALESCE(svl.quantity, 0.0)) AS qty_delta,
                    SUM(COALESCE(svl.value, 0.0)) AS value_delta
                FROM stock_valuation_layer svl
                WHERE svl.create_date >= DATE '2026-01-01'
                  AND svl.create_date < (date_trunc('month', CURRENT_DATE)::date + INTERVAL '1 month')
                GROUP BY svl.product_id, date_trunc('month', svl.create_date)::date
            ),
            svl_presence AS (
                SELECT product_id FROM svl_open
                UNION
                SELECT product_id FROM svl_month_delta
            ),
            current_stock_grid AS (
                SELECT
                    mp.source_db,
                    mp.source_product_id,
                    mp.target_product_id,
                    m.period_month,
                    COALESCE(so.qty_open, 0.0) AS qty_open,
                    COALESCE(so.value_open, 0.0) AS value_open,
                    COALESCE(md.qty_delta, 0.0) AS qty_delta,
                    COALESCE(md.value_delta, 0.0) AS value_delta,
                    (sp.product_id IS NOT NULL) AS has_value
                FROM mapped_products mp
                JOIN months_2026 m ON TRUE
                LEFT JOIN svl_open so
                    ON so.product_id = mp.target_product_id
                LEFT JOIN svl_month_delta md
                    ON md.product_id = mp.target_product_id
                   AND md.period_month = m.period_month
                LEFT JOIN svl_presence sp
                    ON sp.product_id = mp.target_product_id
                WHERE mp.target_product_id IS NOT NULL
            ),
            current_stock_mapped AS (
                SELECT
                    source_db,
                    source_product_id,
                    period_month,
                    CASE
                        WHEN has_value THEN
                            qty_open + SUM(qty_delta) OVER (
                                PARTITION BY source_db, source_product_id
                                ORDER BY period_month
                                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                            )
                        ELSE NULL
                    END AS current_stock_close_qty,
                    CASE
                        WHEN has_value THEN
                            value_open + SUM(value_delta) OVER (
                                PARTITION BY source_db, source_product_id
                                ORDER BY period_month
                                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                            )
                        ELSE NULL
                    END AS current_stock_close_value,
                    has_value AS current_value_available
                FROM current_stock_grid
            ),
            """
        else:
            current_stock_cte = """
            current_stock_mapped AS (
                SELECT
                    mp.source_db,
                    mp.source_product_id,
                    m.period_month,
                    NULL::double precision AS current_stock_close_qty,
                    NULL::double precision AS current_stock_close_value,
                    FALSE AS current_value_available
                FROM mapped_products mp
                JOIN months_2026 m ON TRUE
                WHERE mp.target_product_id IS NOT NULL
            ),
            """

        self.env.cr.execute(
            f"""
            CREATE OR REPLACE VIEW {self._table} AS
            WITH mapped_products AS (
                SELECT
                    lpm.source_db,
                    lpm.source_product_id,
                    lpm.source_default_code,
                    lpm.source_barcode,
                    lpm.source_name,
                    lpm.source_category_name,
                    lpm.source_brand_name,
                    lpm.target_product_id,
                    lpm.match_status,
                    lpm.match_method,
                    COALESCE(lpm.confidence, 0.0) AS confidence,
                    COALESCE(lpm.manual_override, FALSE) AS manual_override
                FROM legacy_product_map lpm
            ),
            legacy_facts AS (
                SELECT
                    lmf.source_db,
                    lmf.source_product_id,
                    lmf.period_month,
                    COALESCE(NULLIF(lmf.warehouse_key, ''), 'all') AS warehouse_key,
                    lmf.source_default_code,
                    lmf.source_barcode,
                    lmf.source_name,
                    lmf.source_category_name,
                    lmf.source_brand_name,
                    COALESCE(lmf.legacy_sales_qty, 0.0) AS legacy_sales_qty,
                    COALESCE(lmf.legacy_sales_amount, 0.0) AS legacy_sales_amount,
                    lmf.legacy_stock_close_qty,
                    lmf.legacy_stock_close_value,
                    COALESCE(lmf.value_available, FALSE) AS legacy_value_available
                FROM legacy_product_month_fact lmf
            ),
            months_2026 AS (
                SELECT generate_series(
                    DATE '2026-01-01',
                    GREATEST(DATE '2026-01-01', date_trunc('month', CURRENT_DATE)::date),
                    INTERVAL '1 month'
                )::date AS period_month
            ),
            current_sales AS (
                SELECT
                    aml.product_id,
                    date_trunc('month', COALESCE(am.invoice_date, am.date))::date AS period_month,
                    SUM(
                        CASE
                            WHEN am.move_type = 'out_refund' THEN -ABS(COALESCE(aml.quantity, 0.0))
                            ELSE ABS(COALESCE(aml.quantity, 0.0))
                        END
                    ) AS current_sales_qty,
                    SUM(
                        CASE
                            WHEN am.move_type = 'out_refund' THEN -ABS(COALESCE(aml.price_subtotal, 0.0))
                            ELSE ABS(COALESCE(aml.price_subtotal, 0.0))
                        END
                    ) AS current_sales_amount
                FROM account_move_line aml
                JOIN account_move am
                    ON am.id = aml.move_id
                WHERE aml.product_id IS NOT NULL
                  AND aml.display_type IS NULL
                  AND am.state = 'posted'
                  AND am.move_type IN ('out_invoice', 'out_refund')
                  AND COALESCE(am.invoice_date, am.date) >= DATE '2026-01-01'
                GROUP BY aml.product_id, date_trunc('month', COALESCE(am.invoice_date, am.date))::date
            ),
            current_sales_mapped AS (
                SELECT
                    mp.source_db,
                    mp.source_product_id,
                    cs.period_month,
                    cs.current_sales_qty,
                    cs.current_sales_amount
                FROM mapped_products mp
                JOIN current_sales cs
                    ON cs.product_id = mp.target_product_id
                WHERE mp.target_product_id IS NOT NULL
            ),
            {current_stock_cte}
            current_metrics AS (
                SELECT
                    COALESCE(csm.source_db, cst.source_db) AS source_db,
                    COALESCE(csm.source_product_id, cst.source_product_id) AS source_product_id,
                    COALESCE(csm.period_month, cst.period_month) AS period_month,
                    'all'::text AS warehouse_key,
                    COALESCE(csm.current_sales_qty, 0.0) AS current_sales_qty,
                    COALESCE(csm.current_sales_amount, 0.0) AS current_sales_amount,
                    cst.current_stock_close_qty,
                    cst.current_stock_close_value,
                    COALESCE(cst.current_value_available, FALSE) AS current_value_available
                FROM current_sales_mapped csm
                FULL OUTER JOIN current_stock_mapped cst
                    ON cst.source_db = csm.source_db
                   AND cst.source_product_id = csm.source_product_id
                   AND cst.period_month = csm.period_month
            ),
            combined AS (
                SELECT
                    COALESCE(lf.source_db, cm.source_db) AS source_db,
                    COALESCE(lf.source_product_id, cm.source_product_id) AS source_product_id,
                    COALESCE(lf.period_month, cm.period_month) AS period_month,
                    COALESCE(lf.warehouse_key, cm.warehouse_key, 'all') AS warehouse_key,
                    COALESCE(lf.source_default_code, mp.source_default_code, lp.default_code) AS source_default_code,
                    COALESCE(lf.source_barcode, mp.source_barcode, lp.barcode) AS source_barcode,
                    COALESCE(lf.source_name, mp.source_name, lp.name) AS source_name,
                    COALESCE(lf.source_category_name, mp.source_category_name, lp.product_category_name) AS source_category_name,
                    COALESCE(lf.source_brand_name, mp.source_brand_name, lp.source_brand_name) AS source_brand_name,
                    mp.target_product_id,
                    pp.product_tmpl_id AS target_product_tmpl_id,
                    pp.default_code AS target_default_code,
                    pp.barcode AS target_barcode,
                    pt.name AS target_name,
                    mp.match_status,
                    mp.match_method,
                    COALESCE(mp.confidence, 0.0) AS confidence,
                    COALESCE(mp.manual_override, FALSE) AS manual_override,
                    COALESCE(lf.legacy_sales_qty, 0.0) AS legacy_sales_qty,
                    COALESCE(lf.legacy_sales_amount, 0.0) AS legacy_sales_amount,
                    lf.legacy_stock_close_qty,
                    lf.legacy_stock_close_value,
                    COALESCE(cm.current_sales_qty, 0.0) AS current_sales_qty,
                    COALESCE(cm.current_sales_amount, 0.0) AS current_sales_amount,
                    cm.current_stock_close_qty,
                    cm.current_stock_close_value,
                    COALESCE(cm.current_sales_qty, 0.0) - COALESCE(lf.legacy_sales_qty, 0.0) AS delta_sales_qty,
                    COALESCE(cm.current_sales_amount, 0.0) - COALESCE(lf.legacy_sales_amount, 0.0) AS delta_sales_amount,
                    CASE
                        WHEN cm.current_stock_close_value IS NULL OR lf.legacy_stock_close_value IS NULL THEN NULL
                        ELSE cm.current_stock_close_value - lf.legacy_stock_close_value
                    END AS delta_stock_value,
                    (COALESCE(lf.legacy_value_available, FALSE) AND COALESCE(cm.current_value_available, FALSE)) AS value_available,
                    (lf.source_product_id IS NOT NULL) AS has_legacy_data,
                    (cm.source_product_id IS NOT NULL) AS has_current_data
                FROM legacy_facts lf
                FULL OUTER JOIN current_metrics cm
                    ON cm.source_db = lf.source_db
                   AND cm.source_product_id = lf.source_product_id
                   AND cm.period_month = lf.period_month
                   AND cm.warehouse_key = lf.warehouse_key
                LEFT JOIN mapped_products mp
                    ON mp.source_db = COALESCE(lf.source_db, cm.source_db)
                   AND mp.source_product_id = COALESCE(lf.source_product_id, cm.source_product_id)
                LEFT JOIN legacy_product lp
                    ON lp.source_db = COALESCE(lf.source_db, cm.source_db)
                   AND lp.source_product_id = COALESCE(lf.source_product_id, cm.source_product_id)
                LEFT JOIN product_product pp
                    ON pp.id = mp.target_product_id
                LEFT JOIN product_template pt
                    ON pt.id = pp.product_tmpl_id
            )
            SELECT
                ROW_NUMBER() OVER (
                    ORDER BY
                        period_month DESC,
                        source_default_code NULLS LAST,
                        source_name NULLS LAST,
                        source_product_id
                ) AS id,
                source_db,
                source_product_id,
                period_month,
                warehouse_key,
                source_default_code,
                source_barcode,
                source_name,
                source_category_name,
                source_brand_name,
                target_product_id,
                target_product_tmpl_id,
                target_default_code,
                target_barcode,
                target_name,
                match_status,
                match_method,
                confidence,
                manual_override,
                legacy_sales_qty,
                legacy_sales_amount,
                legacy_stock_close_qty,
                legacy_stock_close_value,
                current_sales_qty,
                current_sales_amount,
                current_stock_close_qty,
                current_stock_close_value,
                delta_sales_qty,
                delta_sales_amount,
                delta_stock_value,
                value_available,
                has_legacy_data,
                has_current_data
            FROM combined
            WHERE source_product_id IS NOT NULL
            """
        )

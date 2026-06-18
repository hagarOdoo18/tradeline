from odoo import api, fields, models, tools
from odoo.osv import expression
from odoo.tools.safe_eval import safe_eval


MATCH_STATUS_SELECTION = [
    ("matched", "Matched"),
    ("review", "Needs Review"),
    ("unmatched", "Unmatched"),
]

MATCH_METHOD_SELECTION = [
    ("auto_barcode", "Auto (Item Code / Barcode)"),
    ("auto_code", "Auto (Item Code)"),
    ("manual", "Manual"),
    ("none", "None"),
]

BASELINE_MODE_SELECTION = [
    ("yoy", "Same Month Last Year"),
    ("custom_legacy", "Custom Legacy Month"),
    ("previous_current", "Previous Current Month"),
]

BUCKET_MATCH_RULE = "same_name_prefix5"
BUCKET_BASELINE_MATCH_RULE = "prefix5_only"


def _sql_norm_name(expr):
    return f"NULLIF(BTRIM(REGEXP_REPLACE(LOWER(COALESCE({expr}, '')), '[^a-z0-9]+', ' ', 'g')), '')"


def _sql_norm_code(expr):
    return f"NULLIF(REGEXP_REPLACE(UPPER(COALESCE({expr}, '')), '[^A-Z0-9]+', '', 'g'), '')"


def _sql_clean_code_text(expr):
    return (
        "CASE "
        f"WHEN {expr} IS NULL THEN NULL "
        f"WHEN LOWER(BTRIM(COALESCE({expr}, ''))) IN ('', 'false', 'none', 'null') THEN NULL "
        f"ELSE BTRIM({expr}) "
        "END"
    )


def _sql_first_available_code(*exprs):
    cleaned = ", ".join(_sql_clean_code_text(expr) for expr in exprs)
    return f"COALESCE({cleaned})"


def _sql_prefix(expr, length=5):
    return f"LEFT(COALESCE({_sql_norm_code(expr)}, ''), {int(length)})"


def _sql_bucket_key(name_expr, code_expr, length=5):
    name_norm = _sql_norm_name(name_expr)
    prefix = _sql_prefix(code_expr, length)
    return (
        "CASE "
        f"WHEN {name_norm} IS NULL AND NULLIF({prefix}, '') IS NULL THEN NULL "
        f"WHEN {name_norm} IS NULL THEN {prefix} "
        f"WHEN NULLIF({prefix}, '') IS NULL THEN {name_norm} "
        f"ELSE {name_norm} || '|' || {prefix} "
        "END"
    )


def _sql_bucket_key_prefix_only(code_expr, length=5):
    prefix = _sql_prefix(code_expr, length)
    return f"CASE WHEN NULLIF({prefix}, '') IS NULL THEN NULL ELSE {prefix} END"


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
    legacy_return_qty = fields.Float()
    legacy_return_amount = fields.Float()
    legacy_discount_amount = fields.Float()
    legacy_gross_sales_amount = fields.Float()
    legacy_net_sales_amount = fields.Float()
    legacy_asp = fields.Float()
    legacy_cogs_amount = fields.Float()
    legacy_margin_amount = fields.Float(string="Legacy Net Margin $")
    legacy_margin_pct = fields.Float(string="Legacy Net Margin %")
    legacy_cost_available = fields.Boolean(default=False, index=True)
    legacy_cost_source = fields.Char()
    legacy_margin_comparable = fields.Boolean(default=False, index=True)
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
    source_bucket_code = fields.Char(readonly=True)
    target_bucket_code = fields.Char(readonly=True)
    source_code_prefix5 = fields.Char(readonly=True)
    target_code_prefix5 = fields.Char(readonly=True)
    bucket_name = fields.Char(readonly=True)
    bucket_key = fields.Char(readonly=True)
    bucket_match_rule = fields.Char(readonly=True)

    match_status = fields.Selection(selection=MATCH_STATUS_SELECTION, readonly=True)
    match_method = fields.Selection(selection=MATCH_METHOD_SELECTION, readonly=True)
    confidence = fields.Float(readonly=True)
    manual_override = fields.Boolean(readonly=True)
    source_match_identifier = fields.Char(readonly=True)
    target_match_identifier = fields.Char(readonly=True)
    source_match_identifier_type = fields.Char(readonly=True)
    target_match_identifier_type = fields.Char(readonly=True)
    match_strategy = fields.Char(readonly=True)
    candidate_count = fields.Integer(readonly=True)

    legacy_sales_qty = fields.Float(readonly=True)
    legacy_sales_amount = fields.Float(readonly=True)
    legacy_return_qty = fields.Float(readonly=True)
    legacy_return_amount = fields.Float(readonly=True)
    legacy_discount_amount = fields.Float(readonly=True)
    legacy_gross_sales_amount = fields.Float(readonly=True)
    legacy_net_sales_amount = fields.Float(readonly=True)
    legacy_asp = fields.Float(readonly=True)
    legacy_cogs_amount = fields.Float(readonly=True)
    legacy_margin_amount = fields.Float(readonly=True, string="Legacy Net Margin $")
    legacy_margin_pct = fields.Float(readonly=True, string="Legacy Net Margin %")
    legacy_cost_available = fields.Boolean(readonly=True)
    legacy_cost_source = fields.Char(readonly=True)
    legacy_margin_comparable = fields.Boolean(readonly=True)
    legacy_stock_close_qty = fields.Float(readonly=True)
    legacy_stock_close_value = fields.Float(readonly=True)

    current_sales_qty = fields.Float(readonly=True)
    current_sales_amount = fields.Float(readonly=True)
    current_return_qty = fields.Float(readonly=True)
    current_return_amount = fields.Float(readonly=True)
    current_discount_amount = fields.Float(readonly=True)
    current_gross_sales_amount = fields.Float(readonly=True)
    current_net_sales_amount = fields.Float(readonly=True)
    current_asp = fields.Float(readonly=True)
    current_cogs_amount = fields.Float(readonly=True)
    current_margin_amount = fields.Float(readonly=True, string="Current Net Margin $")
    current_margin_pct = fields.Float(readonly=True, string="Current Net Margin %")
    current_cost_available = fields.Boolean(readonly=True)
    current_stock_close_qty = fields.Float(readonly=True)
    current_stock_close_value = fields.Float(readonly=True)

    delta_sales_qty = fields.Float(readonly=True)
    delta_sales_amount = fields.Float(readonly=True)
    delta_return_qty = fields.Float(readonly=True)
    delta_return_amount = fields.Float(readonly=True)
    delta_discount_amount = fields.Float(readonly=True)
    delta_gross_sales_amount = fields.Float(readonly=True)
    delta_net_sales_amount = fields.Float(readonly=True)
    delta_asp = fields.Float(readonly=True)
    delta_cogs_amount = fields.Float(readonly=True)
    delta_margin_amount = fields.Float(readonly=True, string="Net Margin $ Delta")
    delta_margin_pct = fields.Float(readonly=True, string="Net Margin % Delta")
    delta_stock_value = fields.Float(readonly=True)
    margin_comparable = fields.Boolean(readonly=True)
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
                    COALESCE(lpm.manual_override, FALSE) AS manual_override,
                    lpm.source_match_identifier,
                    lpm.target_match_identifier,
                    lpm.source_match_identifier_type,
                    lpm.target_match_identifier_type,
                    lpm.match_strategy,
                    COALESCE(lpm.candidate_count, 0) AS candidate_count
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
                    COALESCE(lmf.legacy_return_qty, 0.0) AS legacy_return_qty,
                    COALESCE(lmf.legacy_return_amount, 0.0) AS legacy_return_amount,
                    COALESCE(lmf.legacy_discount_amount, 0.0) AS legacy_discount_amount,
                    COALESCE(lmf.legacy_gross_sales_amount, 0.0) AS legacy_gross_sales_amount,
                    COALESCE(lmf.legacy_net_sales_amount, COALESCE(lmf.legacy_sales_amount, 0.0)) AS legacy_net_sales_amount,
                    lmf.legacy_asp AS legacy_asp,
                    lmf.legacy_cogs_amount,
                    lmf.legacy_margin_amount,
                    lmf.legacy_margin_pct,
                    COALESCE(lmf.legacy_cost_available, FALSE) AS legacy_cost_available,
                    lmf.legacy_cost_source,
                    COALESCE(lmf.legacy_margin_comparable, FALSE) AS legacy_margin_comparable,
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
            current_report_sales AS (
                SELECT
                    air.product_id,
                    date_trunc('month', air.invoice_date)::date AS period_month,
                    SUM(COALESCE(air.quantity, 0.0)) AS current_sales_qty,
                    SUM(COALESCE(air.price_subtotal, 0.0)) AS current_sales_amount,
                    SUM(
                        CASE
                            WHEN air.move_type = 'out_refund' THEN ABS(COALESCE(air.quantity, 0.0))
                            ELSE 0.0
                        END
                    ) AS current_return_qty,
                    SUM(
                        CASE
                            WHEN air.move_type = 'out_refund' THEN ABS(COALESCE(air.price_subtotal, 0.0))
                            ELSE 0.0
                        END
                    ) AS current_return_amount,
                    SUM(COALESCE(air.inventory_value_untaxed, 0.0)) AS current_cogs_amount,
                    SUM(COALESCE(air.price_margin_taxed, 0.0)) AS current_margin_amount,
                    BOOL_OR(air.inventory_value_untaxed IS NOT NULL OR air.price_margin_taxed IS NOT NULL) AS current_cost_available
                FROM account_invoice_report air
                WHERE air.product_id IS NOT NULL
                  AND air.move_type IN ('out_invoice', 'out_refund')
                  AND air.invoice_date >= DATE '2026-01-01'
                GROUP BY air.product_id, date_trunc('month', air.invoice_date)::date
            ),
            current_line_extras AS (
                SELECT
                    aml.product_id,
                    date_trunc('month', COALESCE(am.invoice_date, am.date))::date AS period_month,
                    SUM(
                        COALESCE(aml.price_unit, 0.0) * COALESCE(aml.signed_quantity, 0.0) * (COALESCE(aml.discount, 0.0) / 100.0)
                    ) AS current_discount_amount,
                    SUM(
                        COALESCE(aml.price_unit, 0.0) * COALESCE(aml.signed_quantity, 0.0)
                    ) AS current_gross_sales_amount
                FROM account_move_line aml
                JOIN account_move am
                    ON am.id = aml.move_id
                WHERE aml.product_id IS NOT NULL
                  AND COALESCE(aml.display_type, 'product') = 'product'
                  AND am.state = 'posted'
                  AND am.move_type IN ('out_invoice', 'out_refund')
                  AND COALESCE(am.invoice_date, am.date) >= DATE '2026-01-01'
                GROUP BY aml.product_id, date_trunc('month', COALESCE(am.invoice_date, am.date))::date
            ),
            current_sales AS (
                SELECT
                    COALESCE(crs.product_id, cle.product_id) AS product_id,
                    COALESCE(crs.period_month, cle.period_month) AS period_month,
                    COALESCE(crs.current_sales_qty, 0.0) AS current_sales_qty,
                    COALESCE(crs.current_sales_amount, 0.0) AS current_sales_amount,
                    COALESCE(crs.current_return_qty, 0.0) AS current_return_qty,
                    COALESCE(crs.current_return_amount, 0.0) AS current_return_amount,
                    COALESCE(cle.current_discount_amount, 0.0) AS current_discount_amount,
                    COALESCE(cle.current_gross_sales_amount, 0.0) AS current_gross_sales_amount,
                    crs.current_cogs_amount,
                    crs.current_margin_amount,
                    COALESCE(crs.current_cost_available, FALSE) AS current_cost_available
                FROM current_report_sales crs
                FULL OUTER JOIN current_line_extras cle
                    ON cle.product_id = crs.product_id
                   AND cle.period_month = crs.period_month
            ),
            current_sales_mapped AS (
                SELECT
                    mp.source_db,
                    mp.source_product_id,
                    cs.period_month,
                    cs.current_sales_qty,
                    cs.current_sales_amount,
                    cs.current_return_qty,
                    cs.current_return_amount,
                    cs.current_discount_amount,
                    cs.current_gross_sales_amount,
                    cs.current_sales_amount AS current_net_sales_amount,
                    CASE
                        WHEN cs.current_sales_qty = 0 THEN NULL
                        ELSE cs.current_sales_amount / cs.current_sales_qty
                    END AS current_asp,
                    cs.current_cogs_amount,
                    cs.current_margin_amount,
                    CASE
                        WHEN cs.current_sales_amount = 0 OR cs.current_margin_amount IS NULL THEN NULL
                        ELSE (cs.current_margin_amount / cs.current_sales_amount) * 100.0
                    END AS current_margin_pct,
                    COALESCE(cs.current_cost_available, FALSE) AS current_cost_available
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
                    COALESCE(csm.current_return_qty, 0.0) AS current_return_qty,
                    COALESCE(csm.current_return_amount, 0.0) AS current_return_amount,
                    COALESCE(csm.current_discount_amount, 0.0) AS current_discount_amount,
                    COALESCE(csm.current_gross_sales_amount, 0.0) AS current_gross_sales_amount,
                    COALESCE(csm.current_net_sales_amount, 0.0) AS current_net_sales_amount,
                    csm.current_asp,
                    csm.current_cogs_amount,
                    csm.current_margin_amount,
                    csm.current_margin_pct,
                    COALESCE(csm.current_cost_available, FALSE) AS current_cost_available,
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
                    {_sql_first_available_code(
                        "lf.source_default_code",
                        "mp.source_default_code",
                        "lp.default_code",
                        "lf.source_barcode",
                        "mp.source_barcode",
                        "lp.barcode",
                    )} AS source_default_code,
                    {_sql_first_available_code(
                        "lf.source_barcode",
                        "mp.source_barcode",
                        "lp.barcode",
                    )} AS source_barcode,
                    COALESCE(lf.source_name, mp.source_name, lp.name) AS source_name,
                    COALESCE(lf.source_category_name, mp.source_category_name, lp.product_category_name) AS source_category_name,
                    COALESCE(lf.source_brand_name, mp.source_brand_name, lp.source_brand_name) AS source_brand_name,
                    mp.target_product_id,
                    pp.product_tmpl_id AS target_product_tmpl_id,
                    pp.default_code AS target_default_code,
                    pp.barcode AS target_barcode,
                    pt.name->>'en_US' AS target_name,
                    mp.match_status,
                    mp.match_method,
                    COALESCE(mp.confidence, 0.0) AS confidence,
                    COALESCE(mp.manual_override, FALSE) AS manual_override,
                    mp.source_match_identifier,
                    mp.target_match_identifier,
                    mp.source_match_identifier_type,
                    mp.target_match_identifier_type,
                    mp.match_strategy,
                    COALESCE(mp.candidate_count, 0) AS candidate_count,
                    COALESCE(lf.legacy_sales_qty, 0.0) AS legacy_sales_qty,
                    COALESCE(lf.legacy_sales_amount, 0.0) AS legacy_sales_amount,
                    COALESCE(lf.legacy_return_qty, 0.0) AS legacy_return_qty,
                    COALESCE(lf.legacy_return_amount, 0.0) AS legacy_return_amount,
                    COALESCE(lf.legacy_discount_amount, 0.0) AS legacy_discount_amount,
                    COALESCE(lf.legacy_gross_sales_amount, 0.0) AS legacy_gross_sales_amount,
                    COALESCE(lf.legacy_net_sales_amount, COALESCE(lf.legacy_sales_amount, 0.0)) AS legacy_net_sales_amount,
                    lf.legacy_asp,
                    lf.legacy_cogs_amount,
                    lf.legacy_margin_amount,
                    lf.legacy_margin_pct,
                    COALESCE(lf.legacy_cost_available, FALSE) AS legacy_cost_available,
                    lf.legacy_cost_source,
                    COALESCE(lf.legacy_margin_comparable, FALSE) AS legacy_margin_comparable,
                    lf.legacy_stock_close_qty,
                    lf.legacy_stock_close_value,
                    COALESCE(cm.current_sales_qty, 0.0) AS current_sales_qty,
                    COALESCE(cm.current_sales_amount, 0.0) AS current_sales_amount,
                    COALESCE(cm.current_return_qty, 0.0) AS current_return_qty,
                    COALESCE(cm.current_return_amount, 0.0) AS current_return_amount,
                    COALESCE(cm.current_discount_amount, 0.0) AS current_discount_amount,
                    COALESCE(cm.current_gross_sales_amount, 0.0) AS current_gross_sales_amount,
                    COALESCE(cm.current_net_sales_amount, 0.0) AS current_net_sales_amount,
                    cm.current_asp,
                    cm.current_cogs_amount,
                    cm.current_margin_amount,
                    cm.current_margin_pct,
                    COALESCE(cm.current_cost_available, FALSE) AS current_cost_available,
                    cm.current_stock_close_qty,
                    cm.current_stock_close_value,
                    COALESCE(cm.current_sales_qty, 0.0) - COALESCE(lf.legacy_sales_qty, 0.0) AS delta_sales_qty,
                    COALESCE(cm.current_sales_amount, 0.0) - COALESCE(lf.legacy_sales_amount, 0.0) AS delta_sales_amount,
                    COALESCE(cm.current_return_qty, 0.0) - COALESCE(lf.legacy_return_qty, 0.0) AS delta_return_qty,
                    COALESCE(cm.current_return_amount, 0.0) - COALESCE(lf.legacy_return_amount, 0.0) AS delta_return_amount,
                    COALESCE(cm.current_discount_amount, 0.0) - COALESCE(lf.legacy_discount_amount, 0.0) AS delta_discount_amount,
                    COALESCE(cm.current_gross_sales_amount, 0.0) - COALESCE(lf.legacy_gross_sales_amount, 0.0) AS delta_gross_sales_amount,
                    COALESCE(cm.current_net_sales_amount, 0.0) - COALESCE(lf.legacy_net_sales_amount, 0.0) AS delta_net_sales_amount,
                    CASE
                        WHEN cm.current_asp IS NULL OR lf.legacy_asp IS NULL THEN NULL
                        ELSE cm.current_asp - lf.legacy_asp
                    END AS delta_asp,
                    CASE
                        WHEN cm.current_cogs_amount IS NULL OR lf.legacy_cogs_amount IS NULL THEN NULL
                        ELSE cm.current_cogs_amount - lf.legacy_cogs_amount
                    END AS delta_cogs_amount,
                    CASE
                        WHEN cm.current_margin_amount IS NULL OR lf.legacy_margin_amount IS NULL THEN NULL
                        ELSE cm.current_margin_amount - lf.legacy_margin_amount
                    END AS delta_margin_amount,
                    CASE
                        WHEN cm.current_margin_pct IS NULL OR lf.legacy_margin_pct IS NULL THEN NULL
                        ELSE cm.current_margin_pct - lf.legacy_margin_pct
                    END AS delta_margin_pct,
                    CASE
                        WHEN cm.current_stock_close_value IS NULL OR lf.legacy_stock_close_value IS NULL THEN NULL
                        ELSE cm.current_stock_close_value - lf.legacy_stock_close_value
                    END AS delta_stock_value,
                    (COALESCE(lf.legacy_cost_available, FALSE) AND COALESCE(cm.current_cost_available, FALSE)) AS margin_comparable,
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
                {_sql_first_available_code("source_default_code", "source_barcode")} AS source_bucket_code,
                {_sql_first_available_code("target_barcode", "target_default_code")} AS target_bucket_code,
                {_sql_prefix(_sql_first_available_code("source_default_code", "source_barcode"))} AS source_code_prefix5,
                {_sql_prefix(_sql_first_available_code("target_barcode", "target_default_code"))} AS target_code_prefix5,
                COALESCE(NULLIF(source_name, ''), NULLIF(target_name, '')) AS bucket_name,
                {_sql_bucket_key(
                    "COALESCE(NULLIF(source_name, ''), NULLIF(target_name, ''))",
                    _sql_first_available_code("source_default_code", "source_barcode", "target_barcode", "target_default_code"),
                )} AS bucket_key,
                '{BUCKET_MATCH_RULE}'::text AS bucket_match_rule,
                match_status,
                match_method,
                confidence,
                manual_override,
                source_match_identifier,
                target_match_identifier,
                source_match_identifier_type,
                target_match_identifier_type,
                match_strategy,
                candidate_count,
                legacy_sales_qty,
                legacy_sales_amount,
                legacy_return_qty,
                legacy_return_amount,
                legacy_discount_amount,
                legacy_gross_sales_amount,
                legacy_net_sales_amount,
                legacy_asp,
                legacy_cogs_amount,
                legacy_margin_amount,
                legacy_margin_pct,
                legacy_cost_available,
                legacy_cost_source,
                legacy_margin_comparable,
                legacy_stock_close_qty,
                legacy_stock_close_value,
                current_sales_qty,
                current_sales_amount,
                current_return_qty,
                current_return_amount,
                current_discount_amount,
                current_gross_sales_amount,
                current_net_sales_amount,
                current_asp,
                current_cogs_amount,
                current_margin_amount,
                current_margin_pct,
                current_cost_available,
                current_stock_close_qty,
                current_stock_close_value,
                delta_sales_qty,
                delta_sales_amount,
                delta_return_qty,
                delta_return_amount,
                delta_discount_amount,
                delta_gross_sales_amount,
                delta_net_sales_amount,
                delta_asp,
                delta_cogs_amount,
                delta_margin_amount,
                delta_margin_pct,
                delta_stock_value,
                margin_comparable,
                value_available,
                has_legacy_data,
                has_current_data
            FROM combined
            WHERE source_product_id IS NOT NULL
            """
        )


class LegacyCurrentProductCompareBaseline(models.Model):
    _name = "legacy.current.product.compare.baseline"
    _description = "Legacy to Current Product Comparison (Baseline)"
    _auto = False
    _order = "current_period_month desc, baseline_mode, baseline_period_month desc, source_default_code, source_name, id"
    _rec_name = "source_name"

    source_db = fields.Char(readonly=True)
    source_product_id = fields.Integer(readonly=True)
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
    source_bucket_code = fields.Char(readonly=True)
    target_bucket_code = fields.Char(readonly=True)
    source_code_prefix5 = fields.Char(readonly=True)
    target_code_prefix5 = fields.Char(readonly=True)
    bucket_name = fields.Char(readonly=True)
    bucket_key = fields.Char(readonly=True)
    bucket_match_rule = fields.Char(readonly=True)

    match_status = fields.Selection(selection=MATCH_STATUS_SELECTION, readonly=True)
    match_method = fields.Selection(selection=MATCH_METHOD_SELECTION, readonly=True)
    confidence = fields.Float(readonly=True)
    manual_override = fields.Boolean(readonly=True)
    source_match_identifier = fields.Char(readonly=True)
    target_match_identifier = fields.Char(readonly=True)
    source_match_identifier_type = fields.Char(readonly=True)
    target_match_identifier_type = fields.Char(readonly=True)
    match_strategy = fields.Char(readonly=True)
    candidate_count = fields.Integer(readonly=True)

    baseline_mode = fields.Selection(selection=BASELINE_MODE_SELECTION, readonly=True)
    current_period_month = fields.Date(readonly=True)
    baseline_period_month = fields.Date(readonly=True)
    baseline_key = fields.Char(readonly=True)
    is_latest_current_month = fields.Boolean(readonly=True)
    current_sales_qty = fields.Float(readonly=True)
    current_sales_amount = fields.Float(readonly=True)
    current_return_qty = fields.Float(readonly=True)
    current_return_amount = fields.Float(readonly=True)
    current_discount_amount = fields.Float(readonly=True)
    current_gross_sales_amount = fields.Float(readonly=True)
    current_net_sales_amount = fields.Float(readonly=True)
    current_asp = fields.Float(readonly=True)
    current_cogs_amount = fields.Float(readonly=True)
    current_margin_amount = fields.Float(readonly=True, string="Current Net Margin $")
    current_margin_pct = fields.Float(readonly=True, string="Current Net Margin %")
    current_cost_available = fields.Boolean(readonly=True)
    baseline_legacy_sales_qty = fields.Float(readonly=True)
    baseline_legacy_sales_amount = fields.Float(readonly=True)
    baseline_legacy_return_qty = fields.Float(readonly=True)
    baseline_legacy_return_amount = fields.Float(readonly=True)
    baseline_legacy_discount_amount = fields.Float(readonly=True)
    baseline_legacy_gross_sales_amount = fields.Float(readonly=True)
    baseline_legacy_net_sales_amount = fields.Float(readonly=True)
    baseline_legacy_asp = fields.Float(readonly=True)
    baseline_legacy_cogs_amount = fields.Float(readonly=True)
    baseline_legacy_margin_amount = fields.Float(readonly=True, string="Baseline Net Margin $")
    baseline_legacy_margin_pct = fields.Float(readonly=True, string="Baseline Net Margin %")
    baseline_legacy_cost_available = fields.Boolean(readonly=True)
    baseline_legacy_margin_comparable = fields.Boolean(readonly=True)
    delta_sales_qty = fields.Float(readonly=True)
    delta_sales_amount = fields.Float(readonly=True)
    delta_sales_qty_pct = fields.Float(readonly=True)
    delta_sales_amount_pct = fields.Float(readonly=True)
    delta_return_qty = fields.Float(readonly=True)
    delta_return_amount = fields.Float(readonly=True)
    delta_discount_amount = fields.Float(readonly=True)
    delta_gross_sales_amount = fields.Float(readonly=True)
    delta_net_sales_amount = fields.Float(readonly=True)
    delta_asp = fields.Float(readonly=True)
    delta_cogs_amount = fields.Float(readonly=True)
    delta_margin_amount = fields.Float(readonly=True, string="Net Margin $ Delta")
    delta_margin_pct = fields.Float(readonly=True, string="Net Margin % Delta")

    margin_comparable = fields.Boolean(readonly=True)
    baseline_has_legacy_data = fields.Boolean(readonly=True)

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
        self.env.cr.execute(
            f"""
            CREATE OR REPLACE VIEW {self._table} AS
            WITH mapped_products AS (
                SELECT
                    lpm.source_db,
                    lpm.source_product_id,
                    {_sql_first_available_code("lpm.source_default_code", "lpm.source_barcode")} AS source_default_code,
                    {_sql_first_available_code("lpm.source_barcode", "lpm.source_default_code")} AS source_barcode,
                    lpm.source_name,
                    lpm.source_category_name,
                    lpm.source_brand_name,
                    lpm.target_product_id,
                    lpm.match_status,
                    lpm.match_method,
                    COALESCE(lpm.confidence, 0.0) AS confidence,
                    COALESCE(lpm.manual_override, FALSE) AS manual_override,
                    lpm.source_match_identifier,
                    lpm.target_match_identifier,
                    lpm.source_match_identifier_type,
                    lpm.target_match_identifier_type,
                    lpm.match_strategy,
                    COALESCE(lpm.candidate_count, 0) AS candidate_count
                FROM legacy_product_map lpm
                WHERE lpm.target_product_id IS NOT NULL
            ),
            months_current AS (
                SELECT generate_series(
                    DATE '2026-01-01',
                    GREATEST(DATE '2026-01-01', date_trunc('month', CURRENT_DATE)::date),
                    INTERVAL '1 month'
                )::date AS period_month
            ),
            current_report_sales AS (
                SELECT
                    air.product_id,
                    date_trunc('month', air.invoice_date)::date AS period_month,
                    SUM(COALESCE(air.quantity, 0.0)) AS current_sales_qty,
                    SUM(COALESCE(air.price_subtotal, 0.0)) AS current_sales_amount,
                    SUM(
                        CASE
                            WHEN air.move_type = 'out_refund' THEN ABS(COALESCE(air.quantity, 0.0))
                            ELSE 0.0
                        END
                    ) AS current_return_qty,
                    SUM(
                        CASE
                            WHEN air.move_type = 'out_refund' THEN ABS(COALESCE(air.price_subtotal, 0.0))
                            ELSE 0.0
                        END
                    ) AS current_return_amount,
                    SUM(COALESCE(air.inventory_value_untaxed, 0.0)) AS current_cogs_amount,
                    SUM(COALESCE(air.price_margin_taxed, 0.0)) AS current_margin_amount,
                    BOOL_OR(air.inventory_value_untaxed IS NOT NULL OR air.price_margin_taxed IS NOT NULL) AS current_cost_available
                FROM account_invoice_report air
                WHERE air.product_id IS NOT NULL
                  AND air.move_type IN ('out_invoice', 'out_refund')
                  AND air.invoice_date >= DATE '2026-01-01'
                GROUP BY air.product_id, date_trunc('month', air.invoice_date)::date
            ),
            current_line_extras AS (
                SELECT
                    aml.product_id,
                    date_trunc('month', COALESCE(am.invoice_date, am.date))::date AS period_month,
                    SUM(
                        COALESCE(aml.price_unit, 0.0) * COALESCE(aml.signed_quantity, 0.0) * (COALESCE(aml.discount, 0.0) / 100.0)
                    ) AS current_discount_amount,
                    SUM(
                        COALESCE(aml.price_unit, 0.0) * COALESCE(aml.signed_quantity, 0.0)
                    ) AS current_gross_sales_amount
                FROM account_move_line aml
                JOIN account_move am
                    ON am.id = aml.move_id
                WHERE aml.product_id IS NOT NULL
                  AND COALESCE(aml.display_type, 'product') = 'product'
                  AND am.state = 'posted'
                  AND am.move_type IN ('out_invoice', 'out_refund')
                  AND COALESCE(am.invoice_date, am.date) >= DATE '2026-01-01'
                GROUP BY aml.product_id, date_trunc('month', COALESCE(am.invoice_date, am.date))::date
            ),
            current_sales AS (
                SELECT
                    COALESCE(crs.product_id, cle.product_id) AS product_id,
                    COALESCE(crs.period_month, cle.period_month) AS period_month,
                    COALESCE(crs.current_sales_qty, 0.0) AS current_sales_qty,
                    COALESCE(crs.current_sales_amount, 0.0) AS current_sales_amount,
                    COALESCE(crs.current_return_qty, 0.0) AS current_return_qty,
                    COALESCE(crs.current_return_amount, 0.0) AS current_return_amount,
                    COALESCE(cle.current_discount_amount, 0.0) AS current_discount_amount,
                    COALESCE(cle.current_gross_sales_amount, 0.0) AS current_gross_sales_amount,
                    crs.current_cogs_amount,
                    crs.current_margin_amount,
                    COALESCE(crs.current_cost_available, FALSE) AS current_cost_available
                FROM current_report_sales crs
                FULL OUTER JOIN current_line_extras cle
                    ON cle.product_id = crs.product_id
                   AND cle.period_month = crs.period_month
            ),
            current_sales_mapped AS (
                SELECT
                    mp.source_db,
                    mp.source_product_id,
                    cs.period_month,
                    cs.current_sales_qty,
                    cs.current_sales_amount,
                    cs.current_return_qty,
                    cs.current_return_amount,
                    cs.current_discount_amount,
                    cs.current_gross_sales_amount,
                    cs.current_sales_amount AS current_net_sales_amount,
                    CASE
                        WHEN cs.current_sales_qty = 0 THEN NULL
                        ELSE cs.current_sales_amount / cs.current_sales_qty
                    END AS current_asp,
                    cs.current_cogs_amount,
                    cs.current_margin_amount,
                    CASE
                        WHEN cs.current_sales_amount = 0 OR cs.current_margin_amount IS NULL THEN NULL
                        ELSE (cs.current_margin_amount / cs.current_sales_amount) * 100.0
                    END AS current_margin_pct,
                    COALESCE(cs.current_cost_available, FALSE) AS current_cost_available
                FROM mapped_products mp
                LEFT JOIN current_sales cs
                    ON cs.product_id = mp.target_product_id
            ),
            current_grid AS (
                SELECT
                    mp.source_db,
                    mp.source_product_id,
                    mp.source_default_code,
                    mp.source_barcode,
                    mp.source_name,
                    mp.source_category_name,
                    mp.source_brand_name,
                    mp.target_product_id,
                    mp.match_status,
                    mp.match_method,
                    mp.confidence,
                    mp.manual_override,
                    mp.source_match_identifier,
                    mp.target_match_identifier,
                    mp.source_match_identifier_type,
                    mp.target_match_identifier_type,
                    mp.match_strategy,
                    COALESCE(mp.candidate_count, 0) AS candidate_count,
                    m.period_month AS current_period_month
                FROM mapped_products mp
                JOIN months_current m ON TRUE
            ),
            current_facts AS (
                SELECT
                    cg.*,
                    COALESCE(csm.current_sales_qty, 0.0) AS current_sales_qty,
                    COALESCE(csm.current_sales_amount, 0.0) AS current_sales_amount,
                    COALESCE(csm.current_return_qty, 0.0) AS current_return_qty,
                    COALESCE(csm.current_return_amount, 0.0) AS current_return_amount,
                    COALESCE(csm.current_discount_amount, 0.0) AS current_discount_amount,
                    COALESCE(csm.current_gross_sales_amount, 0.0) AS current_gross_sales_amount,
                    COALESCE(csm.current_net_sales_amount, 0.0) AS current_net_sales_amount,
                    csm.current_asp,
                    csm.current_cogs_amount,
                    csm.current_margin_amount,
                    csm.current_margin_pct,
                    COALESCE(csm.current_cost_available, FALSE) AS current_cost_available
                FROM current_grid cg
                LEFT JOIN current_sales_mapped csm
                    ON csm.source_db = cg.source_db
                   AND csm.source_product_id = cg.source_product_id
                   AND csm.period_month = cg.current_period_month
            ),
            legacy_sales AS (
                SELECT
                    lmf.source_db,
                    lmf.source_product_id,
                    lmf.period_month::date AS baseline_period_month,
                    SUM(COALESCE(lmf.legacy_sales_qty, 0.0)) AS legacy_sales_qty,
                    SUM(COALESCE(lmf.legacy_sales_amount, 0.0)) AS legacy_sales_amount,
                    SUM(COALESCE(lmf.legacy_return_qty, 0.0)) AS legacy_return_qty,
                    SUM(COALESCE(lmf.legacy_return_amount, 0.0)) AS legacy_return_amount,
                    SUM(COALESCE(lmf.legacy_discount_amount, 0.0)) AS legacy_discount_amount,
                    SUM(COALESCE(lmf.legacy_gross_sales_amount, 0.0)) AS legacy_gross_sales_amount,
                    SUM(COALESCE(lmf.legacy_net_sales_amount, COALESCE(lmf.legacy_sales_amount, 0.0))) AS legacy_net_sales_amount,
                    CASE
                        WHEN SUM(COALESCE(lmf.legacy_sales_qty, 0.0)) = 0 THEN NULL
                        ELSE SUM(COALESCE(lmf.legacy_sales_amount, 0.0)) / SUM(COALESCE(lmf.legacy_sales_qty, 0.0))
                    END AS legacy_asp,
                    CASE
                        WHEN BOOL_OR(COALESCE(lmf.legacy_cost_available, FALSE))
                        THEN SUM(COALESCE(lmf.legacy_cogs_amount, 0.0))
                        ELSE NULL
                    END AS legacy_cogs_amount,
                    CASE
                        WHEN BOOL_OR(COALESCE(lmf.legacy_cost_available, FALSE))
                        THEN SUM(COALESCE(lmf.legacy_margin_amount, COALESCE(lmf.legacy_sales_amount, 0.0) - COALESCE(lmf.legacy_cogs_amount, 0.0)))
                        ELSE NULL
                    END AS legacy_margin_amount,
                    CASE
                        WHEN BOOL_OR(COALESCE(lmf.legacy_cost_available, FALSE))
                             AND SUM(COALESCE(lmf.legacy_sales_amount, 0.0)) != 0
                        THEN (
                            SUM(COALESCE(lmf.legacy_margin_amount, COALESCE(lmf.legacy_sales_amount, 0.0) - COALESCE(lmf.legacy_cogs_amount, 0.0)))
                            / SUM(COALESCE(lmf.legacy_sales_amount, 0.0))
                        ) * 100.0
                        ELSE NULL
                    END AS legacy_margin_pct,
                    BOOL_OR(COALESCE(lmf.legacy_cost_available, FALSE)) AS legacy_cost_available,
                    BOOL_OR(COALESCE(lmf.legacy_margin_comparable, FALSE)) AS legacy_margin_comparable
                FROM legacy_product_month_fact lmf
                WHERE lmf.period_month < DATE '2026-01-01'
                GROUP BY lmf.source_db, lmf.source_product_id, lmf.period_month::date
            ),
            legacy_months AS (
                SELECT DISTINCT baseline_period_month
                FROM legacy_sales
            ),
            yoy_rows AS (
                SELECT
                    cf.*,
                    'yoy'::text AS baseline_mode,
                    (cf.current_period_month - INTERVAL '1 year')::date AS baseline_period_month
                FROM current_facts cf
            ),
            previous_rows AS (
                SELECT
                    cf.*,
                    'previous_current'::text AS baseline_mode,
                    (cf.current_period_month - INTERVAL '1 month')::date AS baseline_period_month
                FROM current_facts cf
                WHERE cf.current_period_month > DATE '2026-01-01'
            ),
            custom_rows AS (
                SELECT
                    cf.*,
                    'custom_legacy'::text AS baseline_mode,
                    lm.baseline_period_month
                FROM current_facts cf
                JOIN legacy_months lm ON TRUE
            ),
            all_rows AS (
                SELECT * FROM yoy_rows
                UNION ALL
                SELECT * FROM previous_rows
                UNION ALL
                SELECT * FROM custom_rows
            ),
            combined AS (
                SELECT
                    ar.source_db,
                    ar.source_product_id,
                    ar.source_default_code,
                    ar.source_barcode,
                    ar.source_name,
                    ar.source_category_name,
                    ar.source_brand_name,
                    ar.target_product_id,
                    pp.product_tmpl_id AS target_product_tmpl_id,
                    pp.default_code AS target_default_code,
                    pp.barcode AS target_barcode,
                    pt.name->>'en_US' AS target_name,
                    ar.match_status,
                    ar.match_method,
                    ar.confidence,
                    ar.manual_override,
                    ar.source_match_identifier,
                    ar.target_match_identifier,
                    ar.source_match_identifier_type,
                    ar.target_match_identifier_type,
                    ar.match_strategy,
                    ar.candidate_count,
                    ar.baseline_mode,
                    ar.current_period_month,
                    ar.baseline_period_month,
                    ar.current_sales_qty,
                    ar.current_sales_amount,
                    ar.current_return_qty,
                    ar.current_return_amount,
                    ar.current_discount_amount,
                    ar.current_gross_sales_amount,
                    ar.current_net_sales_amount,
                    ar.current_asp,
                    ar.current_cogs_amount,
                    ar.current_margin_amount,
                    ar.current_margin_pct,
                    ar.current_cost_available,
                    CASE
                        WHEN ar.baseline_mode = 'previous_current' THEN COALESCE(pf.current_sales_qty, 0.0)
                        ELSE COALESCE(ls.legacy_sales_qty, 0.0)
                    END AS baseline_legacy_sales_qty,
                    CASE
                        WHEN ar.baseline_mode = 'previous_current' THEN COALESCE(pf.current_sales_amount, 0.0)
                        ELSE COALESCE(ls.legacy_sales_amount, 0.0)
                    END AS baseline_legacy_sales_amount,
                    CASE
                        WHEN ar.baseline_mode = 'previous_current' THEN COALESCE(pf.current_return_qty, 0.0)
                        ELSE COALESCE(ls.legacy_return_qty, 0.0)
                    END AS baseline_legacy_return_qty,
                    CASE
                        WHEN ar.baseline_mode = 'previous_current' THEN COALESCE(pf.current_return_amount, 0.0)
                        ELSE COALESCE(ls.legacy_return_amount, 0.0)
                    END AS baseline_legacy_return_amount,
                    CASE
                        WHEN ar.baseline_mode = 'previous_current' THEN COALESCE(pf.current_discount_amount, 0.0)
                        ELSE COALESCE(ls.legacy_discount_amount, 0.0)
                    END AS baseline_legacy_discount_amount,
                    CASE
                        WHEN ar.baseline_mode = 'previous_current' THEN COALESCE(pf.current_gross_sales_amount, 0.0)
                        ELSE COALESCE(ls.legacy_gross_sales_amount, 0.0)
                    END AS baseline_legacy_gross_sales_amount,
                    CASE
                        WHEN ar.baseline_mode = 'previous_current' THEN COALESCE(pf.current_net_sales_amount, 0.0)
                        ELSE COALESCE(ls.legacy_net_sales_amount, 0.0)
                    END AS baseline_legacy_net_sales_amount,
                    CASE
                        WHEN ar.baseline_mode = 'previous_current' THEN pf.current_asp
                        ELSE ls.legacy_asp
                    END AS baseline_legacy_asp,
                    CASE
                        WHEN ar.baseline_mode = 'previous_current' THEN pf.current_cogs_amount
                        ELSE ls.legacy_cogs_amount
                    END AS baseline_legacy_cogs_amount,
                    CASE
                        WHEN ar.baseline_mode = 'previous_current' THEN pf.current_margin_amount
                        ELSE ls.legacy_margin_amount
                    END AS baseline_legacy_margin_amount,
                    CASE
                        WHEN ar.baseline_mode = 'previous_current' THEN pf.current_margin_pct
                        ELSE ls.legacy_margin_pct
                    END AS baseline_legacy_margin_pct,
                    CASE
                        WHEN ar.baseline_mode = 'previous_current' THEN COALESCE(pf.current_cost_available, FALSE)
                        ELSE COALESCE(ls.legacy_cost_available, FALSE)
                    END AS baseline_legacy_cost_available,
                    CASE
                        WHEN ar.baseline_mode = 'previous_current' THEN COALESCE(pf.current_cost_available, FALSE)
                        ELSE COALESCE(ls.legacy_margin_comparable, FALSE)
                    END AS baseline_legacy_margin_comparable,
                    CASE
                        WHEN ar.baseline_mode = 'previous_current' THEN (pf.source_product_id IS NOT NULL)
                        ELSE (ls.source_product_id IS NOT NULL)
                    END AS baseline_has_legacy_data
                FROM all_rows ar
                LEFT JOIN legacy_sales ls
                    ON ls.source_db = ar.source_db
                   AND ls.source_product_id = ar.source_product_id
                   AND ls.baseline_period_month = ar.baseline_period_month
                LEFT JOIN current_facts pf
                    ON ar.baseline_mode = 'previous_current'
                   AND pf.source_db = ar.source_db
                   AND pf.source_product_id = ar.source_product_id
                   AND pf.current_period_month = ar.baseline_period_month
                LEFT JOIN product_product pp
                    ON pp.id = ar.target_product_id
                LEFT JOIN product_template pt
                    ON pt.id = pp.product_tmpl_id
            )
            SELECT
                ROW_NUMBER() OVER (
                    ORDER BY
                        current_period_month DESC,
                        baseline_mode,
                        baseline_period_month DESC,
                        source_default_code NULLS LAST,
                        source_name NULLS LAST,
                        source_product_id
                ) AS id,
                source_db,
                source_product_id,
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
                {_sql_first_available_code("source_default_code", "source_barcode")} AS source_bucket_code,
                {_sql_first_available_code("target_barcode", "target_default_code")} AS target_bucket_code,
                {_sql_prefix(_sql_first_available_code("source_default_code", "source_barcode"))} AS source_code_prefix5,
                {_sql_prefix(_sql_first_available_code("target_barcode", "target_default_code"))} AS target_code_prefix5,
                COALESCE(NULLIF(source_name, ''), NULLIF(target_name, '')) AS bucket_name,
                {_sql_bucket_key(
                    "COALESCE(NULLIF(source_name, ''), NULLIF(target_name, ''))",
                    _sql_first_available_code("source_default_code", "source_barcode", "target_barcode", "target_default_code"),
                )} AS bucket_key,
                '{BUCKET_MATCH_RULE}'::text AS bucket_match_rule,
                match_status,
                match_method,
                confidence,
                manual_override,
                source_match_identifier,
                target_match_identifier,
                source_match_identifier_type,
                target_match_identifier_type,
                match_strategy,
                candidate_count,
                baseline_mode,
                current_period_month,
                baseline_period_month,
                CASE
                    WHEN baseline_mode = 'yoy' THEN to_char(baseline_period_month, 'YYYY-MM')
                    WHEN baseline_mode = 'previous_current' THEN to_char(current_period_month, 'YYYY-MM') || ' vs prev ' || to_char(baseline_period_month, 'YYYY-MM')
                    ELSE to_char(current_period_month, 'YYYY-MM') || ' vs ' || to_char(baseline_period_month, 'YYYY-MM')
                END AS baseline_key,
                (current_period_month = (SELECT MAX(period_month) FROM months_current)) AS is_latest_current_month,
                current_sales_qty,
                current_sales_amount,
                current_return_qty,
                current_return_amount,
                current_discount_amount,
                current_gross_sales_amount,
                current_net_sales_amount,
                current_asp,
                current_cogs_amount,
                current_margin_amount,
                current_margin_pct,
                current_cost_available,
                baseline_legacy_sales_qty,
                baseline_legacy_sales_amount,
                baseline_legacy_return_qty,
                baseline_legacy_return_amount,
                baseline_legacy_discount_amount,
                baseline_legacy_gross_sales_amount,
                baseline_legacy_net_sales_amount,
                baseline_legacy_asp,
                baseline_legacy_cogs_amount,
                baseline_legacy_margin_amount,
                baseline_legacy_margin_pct,
                baseline_legacy_cost_available,
                baseline_legacy_margin_comparable,
                current_sales_qty - baseline_legacy_sales_qty AS delta_sales_qty,
                current_sales_amount - baseline_legacy_sales_amount AS delta_sales_amount,
                CASE
                    WHEN baseline_legacy_sales_qty = 0 THEN NULL
                    ELSE ((current_sales_qty - baseline_legacy_sales_qty) / baseline_legacy_sales_qty) * 100.0
                END AS delta_sales_qty_pct,
                CASE
                    WHEN baseline_legacy_sales_amount = 0 THEN NULL
                    ELSE ((current_sales_amount - baseline_legacy_sales_amount) / baseline_legacy_sales_amount) * 100.0
                END AS delta_sales_amount_pct,
                current_return_qty - baseline_legacy_return_qty AS delta_return_qty,
                current_return_amount - baseline_legacy_return_amount AS delta_return_amount,
                current_discount_amount - baseline_legacy_discount_amount AS delta_discount_amount,
                current_gross_sales_amount - baseline_legacy_gross_sales_amount AS delta_gross_sales_amount,
                current_net_sales_amount - baseline_legacy_net_sales_amount AS delta_net_sales_amount,
                CASE
                    WHEN current_asp IS NULL OR baseline_legacy_asp IS NULL THEN NULL
                    ELSE current_asp - baseline_legacy_asp
                END AS delta_asp,
                CASE
                    WHEN current_cogs_amount IS NULL OR baseline_legacy_cogs_amount IS NULL THEN NULL
                    ELSE current_cogs_amount - baseline_legacy_cogs_amount
                END AS delta_cogs_amount,
                CASE
                    WHEN current_margin_amount IS NULL OR baseline_legacy_margin_amount IS NULL THEN NULL
                    ELSE current_margin_amount - baseline_legacy_margin_amount
                END AS delta_margin_amount,
                CASE
                    WHEN current_margin_pct IS NULL OR baseline_legacy_margin_pct IS NULL THEN NULL
                    ELSE current_margin_pct - baseline_legacy_margin_pct
                END AS delta_margin_pct,
                (baseline_legacy_cost_available AND current_cost_available) AS margin_comparable,
                baseline_has_legacy_data
            FROM combined
            """
        )


class LegacyCurrentProductCompareBucketBaseline(models.Model):
    _name = "legacy.current.product.compare.bucket.baseline"
    _description = "Legacy to Current Product Comparison (Bucket Baseline)"
    _auto = False
    _order = "current_period_month desc, baseline_mode, baseline_period_month desc, bucket_name, id"
    _rec_name = "bucket_name"

    source_db = fields.Char(readonly=True)
    baseline_mode = fields.Selection(selection=BASELINE_MODE_SELECTION, readonly=True)
    current_period_month = fields.Date(readonly=True)
    baseline_period_month = fields.Date(readonly=True)
    baseline_key = fields.Char(readonly=True)
    is_latest_current_month = fields.Boolean(readonly=True)
    is_latest_legacy_month = fields.Boolean(readonly=True)

    bucket_name = fields.Char(readonly=True)
    bucket_key = fields.Char(readonly=True)
    bucket_code_prefix5 = fields.Char(readonly=True)
    bucket_match_rule = fields.Char(readonly=True)
    source_category_name = fields.Char(readonly=True)
    source_brand_name = fields.Char(readonly=True)
    sample_source_item_code = fields.Char(readonly=True)
    sample_target_item_code = fields.Char(readonly=True)
    legacy_product_count = fields.Integer(readonly=True)
    current_product_count = fields.Integer(readonly=True)

    current_sales_qty = fields.Float(readonly=True)
    current_sales_amount = fields.Float(readonly=True)
    current_return_qty = fields.Float(readonly=True)
    current_return_amount = fields.Float(readonly=True)
    current_discount_amount = fields.Float(readonly=True)
    current_gross_sales_amount = fields.Float(readonly=True)
    current_net_sales_amount = fields.Float(readonly=True)
    current_asp = fields.Float(readonly=True)
    current_cogs_amount = fields.Float(readonly=True)
    current_margin_amount = fields.Float(readonly=True, string="Current Net Margin $")
    current_margin_pct = fields.Float(readonly=True, string="Current Net Margin %")
    current_cost_available = fields.Boolean(readonly=True)

    baseline_legacy_sales_qty = fields.Float(readonly=True)
    baseline_legacy_sales_amount = fields.Float(readonly=True)
    baseline_legacy_return_qty = fields.Float(readonly=True)
    baseline_legacy_return_amount = fields.Float(readonly=True)
    baseline_legacy_discount_amount = fields.Float(readonly=True)
    baseline_legacy_gross_sales_amount = fields.Float(readonly=True)
    baseline_legacy_net_sales_amount = fields.Float(readonly=True)
    baseline_legacy_asp = fields.Float(readonly=True)
    baseline_legacy_cogs_amount = fields.Float(readonly=True)
    baseline_legacy_margin_amount = fields.Float(readonly=True, string="Baseline Net Margin $")
    baseline_legacy_margin_pct = fields.Float(readonly=True, string="Baseline Net Margin %")
    baseline_legacy_cost_available = fields.Boolean(readonly=True)
    baseline_legacy_margin_comparable = fields.Boolean(readonly=True)

    delta_sales_qty = fields.Float(readonly=True)
    delta_sales_amount = fields.Float(readonly=True)
    delta_sales_qty_pct = fields.Float(readonly=True)
    delta_sales_amount_pct = fields.Float(readonly=True)
    delta_return_qty = fields.Float(readonly=True)
    delta_return_amount = fields.Float(readonly=True)
    delta_discount_amount = fields.Float(readonly=True)
    delta_gross_sales_amount = fields.Float(readonly=True)
    delta_net_sales_amount = fields.Float(readonly=True)
    delta_asp = fields.Float(readonly=True)
    delta_cogs_amount = fields.Float(readonly=True)
    delta_margin_amount = fields.Float(readonly=True, string="Net Margin $ Delta")
    delta_margin_pct = fields.Float(readonly=True, string="Net Margin % Delta")

    margin_comparable = fields.Boolean(readonly=True)
    baseline_has_legacy_data = fields.Boolean(readonly=True)
    has_current_data = fields.Boolean(readonly=True)

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        legacy_code_expr = _sql_first_available_code("lmf.source_default_code", "lmf.source_barcode")
        current_code_expr = _sql_first_available_code("pp.barcode", "pp.default_code")
        legacy_bucket_key = _sql_bucket_key_prefix_only(legacy_code_expr)
        current_bucket_key = _sql_bucket_key_prefix_only(current_code_expr)
        legacy_prefix5 = _sql_prefix(legacy_code_expr)
        current_prefix5 = _sql_prefix(current_code_expr)
        self.env.cr.execute(
            f"""
            CREATE OR REPLACE VIEW {self._table} AS
            WITH months_current AS (
                SELECT generate_series(
                    DATE '2026-01-01',
                    GREATEST(DATE '2026-01-01', date_trunc('month', CURRENT_DATE)::date),
                    INTERVAL '1 month'
                )::date AS period_month
            ),
            legacy_sales AS (
                SELECT
                    MAX(lmf.source_db) AS source_db,
                    lmf.period_month::date AS baseline_period_month,
                    {legacy_bucket_key} AS bucket_key,
                    MIN(COALESCE(NULLIF(lmf.source_name, ''), NULLIF(lmf.source_default_code, ''), NULLIF(lmf.source_barcode, ''), '[No Name]')) AS bucket_name,
                    {legacy_prefix5} AS bucket_code_prefix5,
                    MIN(lmf.source_category_name) AS source_category_name,
                    MIN(lmf.source_brand_name) AS source_brand_name,
                    MIN({legacy_code_expr}) AS sample_source_item_code,
                    COUNT(DISTINCT lmf.source_product_id) AS legacy_product_count,
                    SUM(COALESCE(lmf.legacy_sales_qty, 0.0)) AS legacy_sales_qty,
                    SUM(COALESCE(lmf.legacy_sales_amount, 0.0)) AS legacy_sales_amount,
                    SUM(COALESCE(lmf.legacy_return_qty, 0.0)) AS legacy_return_qty,
                    SUM(COALESCE(lmf.legacy_return_amount, 0.0)) AS legacy_return_amount,
                    SUM(COALESCE(lmf.legacy_discount_amount, 0.0)) AS legacy_discount_amount,
                    SUM(COALESCE(lmf.legacy_gross_sales_amount, 0.0)) AS legacy_gross_sales_amount,
                    SUM(COALESCE(lmf.legacy_net_sales_amount, COALESCE(lmf.legacy_sales_amount, 0.0))) AS legacy_net_sales_amount,
                    CASE
                        WHEN SUM(COALESCE(lmf.legacy_sales_qty, 0.0)) = 0 THEN NULL
                        ELSE SUM(COALESCE(lmf.legacy_sales_amount, 0.0)) / SUM(COALESCE(lmf.legacy_sales_qty, 0.0))
                    END AS legacy_asp,
                    CASE
                        WHEN BOOL_OR(COALESCE(lmf.legacy_cost_available, FALSE))
                        THEN SUM(COALESCE(lmf.legacy_cogs_amount, 0.0))
                        ELSE NULL
                    END AS legacy_cogs_amount,
                    CASE
                        WHEN BOOL_OR(COALESCE(lmf.legacy_cost_available, FALSE))
                        THEN SUM(COALESCE(lmf.legacy_margin_amount, COALESCE(lmf.legacy_sales_amount, 0.0) - COALESCE(lmf.legacy_cogs_amount, 0.0)))
                        ELSE NULL
                    END AS legacy_margin_amount,
                    CASE
                        WHEN BOOL_OR(COALESCE(lmf.legacy_cost_available, FALSE))
                             AND SUM(COALESCE(lmf.legacy_sales_amount, 0.0)) != 0
                        THEN (
                            SUM(COALESCE(lmf.legacy_margin_amount, COALESCE(lmf.legacy_sales_amount, 0.0) - COALESCE(lmf.legacy_cogs_amount, 0.0)))
                            / SUM(COALESCE(lmf.legacy_sales_amount, 0.0))
                        ) * 100.0
                        ELSE NULL
                    END AS legacy_margin_pct,
                    BOOL_OR(COALESCE(lmf.legacy_cost_available, FALSE)) AS legacy_cost_available,
                    BOOL_OR(COALESCE(lmf.legacy_margin_comparable, FALSE)) AS legacy_margin_comparable
                FROM legacy_product_month_fact lmf
                WHERE lmf.period_month < DATE '2026-01-01'
                GROUP BY lmf.period_month::date, {legacy_bucket_key}, {legacy_prefix5}
                HAVING {legacy_bucket_key} IS NOT NULL
            ),
            current_report_sales AS (
                SELECT
                    date_trunc('month', air.invoice_date)::date AS current_period_month,
                    {current_bucket_key} AS bucket_key,
                    MIN(COALESCE(NULLIF(pt.name->>'en_US', ''), NULLIF(pp.default_code, ''), NULLIF(pp.barcode, ''), '[No Name]')) AS bucket_name,
                    {current_prefix5} AS bucket_code_prefix5,
                    MIN({current_code_expr}) AS sample_target_item_code,
                    COUNT(DISTINCT air.product_id) AS current_product_count,
                    SUM(COALESCE(air.quantity, 0.0)) AS current_sales_qty,
                    SUM(COALESCE(air.price_subtotal, 0.0)) AS current_sales_amount,
                    SUM(
                        CASE
                            WHEN air.move_type = 'out_refund' THEN ABS(COALESCE(air.quantity, 0.0))
                            ELSE 0.0
                        END
                    ) AS current_return_qty,
                    SUM(
                        CASE
                            WHEN air.move_type = 'out_refund' THEN ABS(COALESCE(air.price_subtotal, 0.0))
                            ELSE 0.0
                        END
                    ) AS current_return_amount,
                    SUM(COALESCE(air.inventory_value_untaxed, 0.0)) AS current_cogs_amount,
                    SUM(COALESCE(air.price_margin_taxed, 0.0)) AS current_margin_amount,
                    BOOL_OR(air.inventory_value_untaxed IS NOT NULL OR air.price_margin_taxed IS NOT NULL) AS current_cost_available
                FROM account_invoice_report air
                JOIN product_product pp
                    ON pp.id = air.product_id
                JOIN product_template pt
                    ON pt.id = pp.product_tmpl_id
                WHERE air.product_id IS NOT NULL
                  AND air.move_type IN ('out_invoice', 'out_refund')
                  AND air.invoice_date >= DATE '2026-01-01'
                GROUP BY date_trunc('month', air.invoice_date)::date, {current_bucket_key}, {current_prefix5}
                HAVING {current_bucket_key} IS NOT NULL
            ),
            current_line_extras AS (
                SELECT
                    date_trunc('month', COALESCE(am.invoice_date, am.date))::date AS current_period_month,
                    {current_bucket_key} AS bucket_key,
                    {current_prefix5} AS bucket_code_prefix5,
                    SUM(
                        COALESCE(aml.price_unit, 0.0) * COALESCE(aml.signed_quantity, 0.0) * (COALESCE(aml.discount, 0.0) / 100.0)
                    ) AS current_discount_amount,
                    SUM(
                        COALESCE(aml.price_unit, 0.0) * COALESCE(aml.signed_quantity, 0.0)
                    ) AS current_gross_sales_amount
                FROM account_move_line aml
                JOIN account_move am
                    ON am.id = aml.move_id
                JOIN product_product pp
                    ON pp.id = aml.product_id
                WHERE aml.product_id IS NOT NULL
                  AND COALESCE(aml.display_type, 'product') = 'product'
                  AND am.state = 'posted'
                  AND am.move_type IN ('out_invoice', 'out_refund')
                  AND COALESCE(am.invoice_date, am.date) >= DATE '2026-01-01'
                GROUP BY date_trunc('month', COALESCE(am.invoice_date, am.date))::date, {current_bucket_key}, {current_prefix5}
                HAVING {current_bucket_key} IS NOT NULL
            ),
            current_sales AS (
                SELECT
                    COALESCE(crs.current_period_month, cle.current_period_month) AS current_period_month,
                    COALESCE(crs.bucket_key, cle.bucket_key) AS bucket_key,
                    crs.bucket_name,
                    COALESCE(crs.bucket_code_prefix5, cle.bucket_code_prefix5) AS bucket_code_prefix5,
                    crs.sample_target_item_code,
                    COALESCE(crs.current_product_count, 0) AS current_product_count,
                    COALESCE(crs.current_sales_qty, 0.0) AS current_sales_qty,
                    COALESCE(crs.current_sales_amount, 0.0) AS current_sales_amount,
                    COALESCE(crs.current_return_qty, 0.0) AS current_return_qty,
                    COALESCE(crs.current_return_amount, 0.0) AS current_return_amount,
                    COALESCE(cle.current_discount_amount, 0.0) AS current_discount_amount,
                    COALESCE(cle.current_gross_sales_amount, 0.0) AS current_gross_sales_amount,
                    crs.current_cogs_amount,
                    crs.current_margin_amount,
                    COALESCE(crs.current_cost_available, FALSE) AS current_cost_available
                FROM current_report_sales crs
                FULL OUTER JOIN current_line_extras cle
                    ON cle.current_period_month = crs.current_period_month
                   AND cle.bucket_key = crs.bucket_key
            ),
            legacy_source_db AS (
                SELECT MAX(source_db) AS source_db
                FROM legacy_product_month_fact
            ),
            legacy_bucket_dim AS (
                SELECT
                    bucket_key,
                    MAX(source_db) AS source_db,
                    MIN(bucket_name) AS bucket_name,
                    MIN(bucket_code_prefix5) AS bucket_code_prefix5,
                    MIN(source_category_name) AS source_category_name,
                    MIN(source_brand_name) AS source_brand_name,
                    MIN(sample_source_item_code) AS sample_source_item_code
                FROM legacy_sales
                GROUP BY bucket_key
            ),
            current_bucket_dim AS (
                SELECT
                    bucket_key,
                    MIN(bucket_name) AS bucket_name,
                    MIN(bucket_code_prefix5) AS bucket_code_prefix5,
                    MIN(sample_target_item_code) AS sample_target_item_code
                FROM current_sales
                GROUP BY bucket_key
            ),
            bucket_dim AS (
                SELECT
                    COALESCE(ld.source_db, ldb.source_db) AS source_db,
                    COALESCE(ld.bucket_key, cd.bucket_key) AS bucket_key,
                    COALESCE(ld.bucket_name, cd.bucket_name) AS bucket_name,
                    COALESCE(ld.bucket_code_prefix5, cd.bucket_code_prefix5) AS bucket_code_prefix5,
                    ld.source_category_name,
                    ld.source_brand_name,
                    ld.sample_source_item_code,
                    cd.sample_target_item_code
                FROM legacy_bucket_dim ld
                FULL OUTER JOIN current_bucket_dim cd
                    ON cd.bucket_key = ld.bucket_key
                CROSS JOIN legacy_source_db ldb
                WHERE COALESCE(ld.bucket_key, cd.bucket_key) IS NOT NULL
            ),
            current_grid AS (
                SELECT
                    bd.source_db,
                    bd.bucket_key,
                    bd.bucket_name,
                    bd.bucket_code_prefix5,
                    bd.source_category_name,
                    bd.source_brand_name,
                    bd.sample_source_item_code,
                    bd.sample_target_item_code,
                    m.period_month AS current_period_month
                FROM bucket_dim bd
                JOIN months_current m ON TRUE
            ),
            current_facts AS (
                SELECT
                    cg.source_db,
                    cg.bucket_key,
                    cg.bucket_name,
                    cg.bucket_code_prefix5,
                    cg.source_category_name,
                    cg.source_brand_name,
                    cg.sample_source_item_code,
                    COALESCE(cs.sample_target_item_code, cg.sample_target_item_code) AS sample_target_item_code,
                    cg.current_period_month,
                    COALESCE(cs.current_product_count, 0) AS current_product_count,
                    COALESCE(cs.current_sales_qty, 0.0) AS current_sales_qty,
                    COALESCE(cs.current_sales_amount, 0.0) AS current_sales_amount,
                    COALESCE(cs.current_return_qty, 0.0) AS current_return_qty,
                    COALESCE(cs.current_return_amount, 0.0) AS current_return_amount,
                    COALESCE(cs.current_discount_amount, 0.0) AS current_discount_amount,
                    COALESCE(cs.current_gross_sales_amount, 0.0) AS current_gross_sales_amount,
                    COALESCE(cs.current_sales_amount, 0.0) AS current_net_sales_amount,
                    CASE
                        WHEN COALESCE(cs.current_sales_qty, 0.0) = 0 THEN NULL
                        ELSE cs.current_sales_amount / cs.current_sales_qty
                    END AS current_asp,
                    cs.current_cogs_amount,
                    cs.current_margin_amount AS current_margin_amount,
                    CASE
                        WHEN cs.current_sales_amount = 0 OR cs.current_margin_amount IS NULL THEN NULL
                        ELSE (cs.current_margin_amount / cs.current_sales_amount) * 100.0
                    END AS current_margin_pct,
                    COALESCE(cs.current_cost_available, FALSE) AS current_cost_available
                FROM current_grid cg
                LEFT JOIN current_sales cs
                    ON cs.bucket_key = cg.bucket_key
                   AND cs.current_period_month = cg.current_period_month
            ),
            legacy_months AS (
                SELECT DISTINCT baseline_period_month
                FROM legacy_sales
            ),
            yoy_rows AS (
                SELECT
                    cf.*,
                    'yoy'::text AS baseline_mode,
                    (cf.current_period_month - INTERVAL '1 year')::date AS baseline_period_month
                FROM current_facts cf
            ),
            previous_rows AS (
                SELECT
                    cf.*,
                    'previous_current'::text AS baseline_mode,
                    (cf.current_period_month - INTERVAL '1 month')::date AS baseline_period_month
                FROM current_facts cf
                WHERE cf.current_period_month > DATE '2026-01-01'
            ),
            custom_rows AS (
                SELECT
                    cf.*,
                    'custom_legacy'::text AS baseline_mode,
                    lm.baseline_period_month
                FROM current_facts cf
                JOIN legacy_months lm ON TRUE
            ),
            all_rows AS (
                SELECT * FROM yoy_rows
                UNION ALL
                SELECT * FROM previous_rows
                UNION ALL
                SELECT * FROM custom_rows
            ),
            combined AS (
                SELECT
                    ar.source_db,
                    ar.baseline_mode,
                    ar.current_period_month,
                    ar.baseline_period_month,
                    ar.bucket_name,
                    ar.bucket_key,
                    ar.bucket_code_prefix5,
                    '{BUCKET_BASELINE_MATCH_RULE}'::text AS bucket_match_rule,
                    ar.source_category_name,
                    ar.source_brand_name,
                    ar.sample_source_item_code,
                    ar.sample_target_item_code,
                    CASE
                        WHEN ar.baseline_mode = 'previous_current' THEN COALESCE(cb.current_product_count, 0)
                        ELSE COALESCE(ls.legacy_product_count, 0)
                    END AS legacy_product_count,
                    COALESCE(ar.current_product_count, 0) AS current_product_count,
                    ar.current_sales_qty,
                    ar.current_sales_amount,
                    ar.current_return_qty,
                    ar.current_return_amount,
                    ar.current_discount_amount,
                    ar.current_gross_sales_amount,
                    ar.current_net_sales_amount,
                    ar.current_asp,
                    ar.current_cogs_amount,
                    ar.current_margin_amount,
                    ar.current_margin_pct,
                    ar.current_cost_available,
                    CASE
                        WHEN ar.baseline_mode = 'previous_current' THEN COALESCE(cb.current_sales_qty, 0.0)
                        ELSE COALESCE(ls.legacy_sales_qty, 0.0)
                    END AS baseline_legacy_sales_qty,
                    CASE
                        WHEN ar.baseline_mode = 'previous_current' THEN COALESCE(cb.current_sales_amount, 0.0)
                        ELSE COALESCE(ls.legacy_sales_amount, 0.0)
                    END AS baseline_legacy_sales_amount,
                    CASE
                        WHEN ar.baseline_mode = 'previous_current' THEN COALESCE(cb.current_return_qty, 0.0)
                        ELSE COALESCE(ls.legacy_return_qty, 0.0)
                    END AS baseline_legacy_return_qty,
                    CASE
                        WHEN ar.baseline_mode = 'previous_current' THEN COALESCE(cb.current_return_amount, 0.0)
                        ELSE COALESCE(ls.legacy_return_amount, 0.0)
                    END AS baseline_legacy_return_amount,
                    CASE
                        WHEN ar.baseline_mode = 'previous_current' THEN COALESCE(cb.current_discount_amount, 0.0)
                        ELSE COALESCE(ls.legacy_discount_amount, 0.0)
                    END AS baseline_legacy_discount_amount,
                    CASE
                        WHEN ar.baseline_mode = 'previous_current' THEN COALESCE(cb.current_gross_sales_amount, 0.0)
                        ELSE COALESCE(ls.legacy_gross_sales_amount, 0.0)
                    END AS baseline_legacy_gross_sales_amount,
                    CASE
                        WHEN ar.baseline_mode = 'previous_current' THEN COALESCE(cb.current_net_sales_amount, 0.0)
                        ELSE COALESCE(ls.legacy_net_sales_amount, 0.0)
                    END AS baseline_legacy_net_sales_amount,
                    CASE
                        WHEN ar.baseline_mode = 'previous_current' THEN cb.current_asp
                        ELSE ls.legacy_asp
                    END AS baseline_legacy_asp,
                    CASE
                        WHEN ar.baseline_mode = 'previous_current' THEN cb.current_cogs_amount
                        ELSE ls.legacy_cogs_amount
                    END AS baseline_legacy_cogs_amount,
                    CASE
                        WHEN ar.baseline_mode = 'previous_current' THEN cb.current_margin_amount
                        ELSE ls.legacy_margin_amount
                    END AS baseline_legacy_margin_amount,
                    CASE
                        WHEN ar.baseline_mode = 'previous_current' THEN cb.current_margin_pct
                        ELSE ls.legacy_margin_pct
                    END AS baseline_legacy_margin_pct,
                    CASE
                        WHEN ar.baseline_mode = 'previous_current' THEN COALESCE(cb.current_cost_available, FALSE)
                        ELSE COALESCE(ls.legacy_cost_available, FALSE)
                    END AS baseline_legacy_cost_available,
                    CASE
                        WHEN ar.baseline_mode = 'previous_current' THEN COALESCE(cb.current_cost_available, FALSE)
                        ELSE COALESCE(ls.legacy_margin_comparable, FALSE)
                    END AS baseline_legacy_margin_comparable,
                    CASE
                        WHEN ar.baseline_mode = 'previous_current' THEN (cb.bucket_key IS NOT NULL)
                        ELSE (ls.bucket_key IS NOT NULL)
                    END AS baseline_has_legacy_data,
                    (
                        COALESCE(ar.current_sales_qty, 0.0) != 0.0
                        OR COALESCE(ar.current_sales_amount, 0.0) != 0.0
                        OR COALESCE(ar.current_return_qty, 0.0) != 0.0
                        OR COALESCE(ar.current_return_amount, 0.0) != 0.0
                    ) AS has_current_data
                FROM all_rows ar
                LEFT JOIN legacy_sales ls
                    ON ls.bucket_key = ar.bucket_key
                   AND ls.baseline_period_month = ar.baseline_period_month
                LEFT JOIN current_facts cb
                    ON ar.baseline_mode = 'previous_current'
                   AND cb.bucket_key = ar.bucket_key
                   AND cb.current_period_month = ar.baseline_period_month
            )
            SELECT
                ROW_NUMBER() OVER (
                    ORDER BY
                        current_period_month DESC,
                        baseline_mode,
                        baseline_period_month DESC,
                        bucket_name NULLS LAST,
                        bucket_key NULLS LAST
                ) AS id,
                source_db,
                baseline_mode,
                current_period_month,
                baseline_period_month,
                CASE
                    WHEN baseline_mode = 'yoy' THEN to_char(baseline_period_month, 'YYYY-MM')
                    WHEN baseline_mode = 'previous_current'
                    THEN to_char(current_period_month, 'YYYY-MM') || ' vs prev ' || to_char(baseline_period_month, 'YYYY-MM')
                    ELSE to_char(current_period_month, 'YYYY-MM') || ' vs ' || to_char(baseline_period_month, 'YYYY-MM')
                END AS baseline_key,
                (current_period_month = (SELECT MAX(period_month) FROM months_current)) AS is_latest_current_month,
                (
                    baseline_period_month = (
                        SELECT MAX(period_month)
                        FROM legacy_product_month_fact
                        WHERE period_month < DATE '2026-01-01'
                    )
                ) AS is_latest_legacy_month,
                bucket_name,
                bucket_key,
                bucket_code_prefix5,
                bucket_match_rule,
                source_category_name,
                source_brand_name,
                sample_source_item_code,
                sample_target_item_code,
                legacy_product_count,
                current_product_count,
                current_sales_qty,
                current_sales_amount,
                current_return_qty,
                current_return_amount,
                current_discount_amount,
                current_gross_sales_amount,
                current_net_sales_amount,
                current_asp,
                current_cogs_amount,
                current_margin_amount,
                current_margin_pct,
                current_cost_available,
                baseline_legacy_sales_qty,
                baseline_legacy_sales_amount,
                baseline_legacy_return_qty,
                baseline_legacy_return_amount,
                baseline_legacy_discount_amount,
                baseline_legacy_gross_sales_amount,
                baseline_legacy_net_sales_amount,
                baseline_legacy_asp,
                baseline_legacy_cogs_amount,
                baseline_legacy_margin_amount,
                baseline_legacy_margin_pct,
                baseline_legacy_cost_available,
                baseline_legacy_margin_comparable,
                current_sales_qty - baseline_legacy_sales_qty AS delta_sales_qty,
                current_sales_amount - baseline_legacy_sales_amount AS delta_sales_amount,
                CASE
                    WHEN baseline_legacy_sales_qty = 0 THEN NULL
                    ELSE ((current_sales_qty - baseline_legacy_sales_qty) / baseline_legacy_sales_qty) * 100.0
                END AS delta_sales_qty_pct,
                CASE
                    WHEN baseline_legacy_sales_amount = 0 THEN NULL
                    ELSE ((current_sales_amount - baseline_legacy_sales_amount) / baseline_legacy_sales_amount) * 100.0
                END AS delta_sales_amount_pct,
                current_return_qty - baseline_legacy_return_qty AS delta_return_qty,
                current_return_amount - baseline_legacy_return_amount AS delta_return_amount,
                current_discount_amount - baseline_legacy_discount_amount AS delta_discount_amount,
                current_gross_sales_amount - baseline_legacy_gross_sales_amount AS delta_gross_sales_amount,
                current_net_sales_amount - baseline_legacy_net_sales_amount AS delta_net_sales_amount,
                CASE
                    WHEN current_asp IS NULL OR baseline_legacy_asp IS NULL THEN NULL
                    ELSE current_asp - baseline_legacy_asp
                END AS delta_asp,
                CASE
                    WHEN current_cogs_amount IS NULL OR baseline_legacy_cogs_amount IS NULL THEN NULL
                    ELSE current_cogs_amount - baseline_legacy_cogs_amount
                END AS delta_cogs_amount,
                CASE
                    WHEN current_margin_amount IS NULL OR baseline_legacy_margin_amount IS NULL THEN NULL
                    ELSE current_margin_amount - baseline_legacy_margin_amount
                END AS delta_margin_amount,
                CASE
                    WHEN current_margin_pct IS NULL OR baseline_legacy_margin_pct IS NULL THEN NULL
                    ELSE current_margin_pct - baseline_legacy_margin_pct
                END AS delta_margin_pct,
                (baseline_legacy_cost_available AND current_cost_available) AS margin_comparable,
                baseline_has_legacy_data,
                has_current_data
            FROM combined
            """
        )


class LegacyCurrentProductHistory(models.Model):
    _name = "legacy.current.product.history"
    _description = "Legacy to Current Product History"
    _auto = False
    _order = "bucket_code_prefix5, period_month, source_system, id"
    _rec_name = "bucket_name"

    source_db = fields.Char(readonly=True)
    period_month = fields.Date(readonly=True)
    source_system = fields.Selection(
        selection=[
            ("legacy", "Legacy"),
            ("current", "Current"),
        ],
        readonly=True,
    )

    bucket_name = fields.Char(readonly=True)
    bucket_key = fields.Char(readonly=True)
    bucket_code_prefix5 = fields.Char(readonly=True)
    sample_item_code = fields.Char(readonly=True)
    source_category_name = fields.Char(readonly=True)
    source_brand_name = fields.Char(readonly=True)
    branch_id = fields.Integer(readonly=True)
    team_id = fields.Integer(readonly=True)
    invoice_user_id = fields.Integer(readonly=True)
    company_id = fields.Integer(readonly=True)
    product_count = fields.Integer(readonly=True)

    sales_qty = fields.Float(readonly=True)
    sales_amount = fields.Float(readonly=True, string="Net Sales Amount")
    return_qty = fields.Float(readonly=True)
    return_amount = fields.Float(readonly=True)
    discount_amount = fields.Float(readonly=True)
    gross_sales_amount = fields.Float(readonly=True)
    asp = fields.Float(readonly=True)
    cogs_amount = fields.Float(readonly=True)
    margin_amount = fields.Float(readonly=True, string="Net Margin $")
    margin_pct = fields.Float(readonly=True, string="Net Margin %")
    cost_available = fields.Boolean(readonly=True)
    margin_comparable = fields.Boolean(readonly=True)

    @api.model
    def _get_invoice_analysis_scope_filter(self):
        if self.env.context.get("legacy_history_skip_invoice_scope"):
            return []

        invoice_filter = self.env["ir.filters"].search(
            [
                ("model_id", "=", "account.invoice.report"),
                ("name", "=", "Invoices Analysis"),
                "|",
                ("user_id", "=", self.env.uid),
                ("user_id", "=", False),
            ],
            order="user_id desc, write_date desc, id desc",
            limit=1,
        )
        if not invoice_filter or not invoice_filter.domain:
            return []

        try:
            raw_domain = safe_eval(
                invoice_filter.domain,
                {"context_today": lambda: fields.Date.context_today(self)},
                {"self": self},
            )
        except Exception:
            return []

        supported_fields = {"branch_id", "team_id", "invoice_user_id", "company_id"}
        current_scope = []

        def _collect_terms(node):
            if isinstance(node, (list, tuple)):
                if len(node) >= 3 and isinstance(node[0], str) and node[0] in supported_fields:
                    current_scope.append(tuple(node[:3]))
                    return
                for child in node:
                    _collect_terms(child)

        _collect_terms(raw_domain)
        if not current_scope:
            return []

        return expression.OR(
            [
                [("source_system", "=", "legacy")],
                expression.AND([[("source_system", "=", "current")], current_scope]),
            ]
        )

    @api.model
    def _search(self, domain, offset=0, limit=None, order=None, *args, **kwargs):
        scope_filter = self._get_invoice_analysis_scope_filter()
        if scope_filter:
            domain = expression.AND([domain or [], scope_filter])
        return super()._search(domain, offset=offset, limit=limit, order=order, *args, **kwargs)

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)

        legacy_code_expr = _sql_first_available_code("lmf.source_default_code", "lmf.source_barcode")
        current_code_expr = _sql_first_available_code("pp.barcode", "pp.default_code")
        legacy_bucket_key = _sql_bucket_key_prefix_only(legacy_code_expr)
        current_bucket_key = _sql_bucket_key_prefix_only(current_code_expr)
        legacy_prefix5 = _sql_prefix(legacy_code_expr)
        current_prefix5 = _sql_prefix(current_code_expr)

        self.env.cr.execute(
            f"""
            CREATE OR REPLACE VIEW {self._table} AS
            WITH legacy_source_db AS (
                SELECT MAX(source_db) AS source_db
                FROM legacy_product_month_fact
            ),
            legacy_sales AS (
                SELECT
                    lmf.period_month::date AS period_month,
                    'legacy'::text AS source_system,
                    {legacy_bucket_key} AS bucket_key,
                    MIN(COALESCE(NULLIF(lmf.source_name, ''), NULLIF(lmf.source_default_code, ''), NULLIF(lmf.source_barcode, ''), '[No Name]')) AS bucket_name,
                    {legacy_prefix5} AS bucket_code_prefix5,
                    MIN(COALESCE(NULLIF(lmf.source_default_code, ''), NULLIF(lmf.source_barcode, ''), '[No Code]')) AS sample_item_code,
                    MIN(lmf.source_category_name) AS source_category_name,
                    MIN(lmf.source_brand_name) AS source_brand_name,
                    NULL::integer AS branch_id,
                    NULL::integer AS team_id,
                    NULL::integer AS invoice_user_id,
                    NULL::integer AS company_id,
                    COUNT(DISTINCT lmf.source_product_id) AS product_count,
                    SUM(COALESCE(lmf.legacy_sales_qty, 0.0)) AS sales_qty,
                    SUM(COALESCE(lmf.legacy_sales_amount, 0.0)) AS sales_amount,
                    SUM(COALESCE(lmf.legacy_return_qty, 0.0)) AS return_qty,
                    SUM(COALESCE(lmf.legacy_return_amount, 0.0)) AS return_amount,
                    SUM(COALESCE(lmf.legacy_discount_amount, 0.0)) AS discount_amount,
                    SUM(COALESCE(lmf.legacy_gross_sales_amount, 0.0)) AS gross_sales_amount,
                    CASE
                        WHEN SUM(COALESCE(lmf.legacy_sales_qty, 0.0)) = 0 THEN NULL
                        ELSE SUM(COALESCE(lmf.legacy_sales_amount, 0.0)) / SUM(COALESCE(lmf.legacy_sales_qty, 0.0))
                    END AS asp,
                    CASE
                        WHEN BOOL_OR(COALESCE(lmf.legacy_cost_available, FALSE))
                        THEN SUM(COALESCE(lmf.legacy_cogs_amount, 0.0))
                        ELSE NULL
                    END AS cogs_amount,
                    CASE
                        WHEN BOOL_OR(COALESCE(lmf.legacy_cost_available, FALSE))
                        THEN SUM(COALESCE(lmf.legacy_margin_amount, COALESCE(lmf.legacy_sales_amount, 0.0) - COALESCE(lmf.legacy_cogs_amount, 0.0)))
                        ELSE NULL
                    END AS margin_amount,
                    CASE
                        WHEN BOOL_OR(COALESCE(lmf.legacy_cost_available, FALSE))
                             AND SUM(COALESCE(lmf.legacy_sales_amount, 0.0)) != 0
                        THEN (
                            SUM(COALESCE(lmf.legacy_margin_amount, COALESCE(lmf.legacy_sales_amount, 0.0) - COALESCE(lmf.legacy_cogs_amount, 0.0)))
                            / SUM(COALESCE(lmf.legacy_sales_amount, 0.0))
                        ) * 100.0
                        ELSE NULL
                    END AS margin_pct,
                    BOOL_OR(COALESCE(lmf.legacy_cost_available, FALSE)) AS cost_available
                FROM legacy_product_month_fact lmf
                WHERE lmf.period_month < DATE '2026-01-01'
                GROUP BY lmf.period_month::date, {legacy_bucket_key}, {legacy_prefix5}
                HAVING {legacy_bucket_key} IS NOT NULL
            ),
            current_report_sales AS (
                SELECT
                    date_trunc('month', air.invoice_date)::date AS period_month,
                    'current'::text AS source_system,
                    {current_bucket_key} AS bucket_key,
                    MIN(COALESCE(NULLIF(pt.name->>'en_US', ''), NULLIF(pp.barcode, ''), NULLIF(pp.default_code, ''), '[No Name]')) AS bucket_name,
                    {current_prefix5} AS bucket_code_prefix5,
                    MIN({current_code_expr}) AS sample_item_code,
                    MIN(pc.complete_name) AS source_category_name,
                    NULL::text AS source_brand_name,
                    air.branch_id AS branch_id,
                    air.team_id AS team_id,
                    air.invoice_user_id AS invoice_user_id,
                    air.company_id AS company_id,
                    COUNT(DISTINCT air.product_id) AS product_count,
                    SUM(COALESCE(air.quantity, 0.0)) AS sales_qty,
                    SUM(COALESCE(air.price_subtotal, 0.0)) AS sales_amount,
                    SUM(
                        CASE
                            WHEN air.move_type = 'out_refund' THEN ABS(COALESCE(air.quantity, 0.0))
                            ELSE 0.0
                        END
                    ) AS return_qty,
                    SUM(
                        CASE
                            WHEN air.move_type = 'out_refund' THEN ABS(COALESCE(air.price_subtotal, 0.0))
                            ELSE 0.0
                        END
                    ) AS return_amount,
                    SUM(COALESCE(air.inventory_value_untaxed, 0.0)) AS cogs_amount,
                    SUM(COALESCE(air.price_margin_taxed, 0.0)) AS margin_amount,
                    BOOL_OR(air.inventory_value_untaxed IS NOT NULL OR air.price_margin_taxed IS NOT NULL) AS cost_available
                FROM account_invoice_report air
                JOIN product_product pp
                    ON pp.id = air.product_id
                JOIN product_template pt
                    ON pt.id = pp.product_tmpl_id
                LEFT JOIN product_category pc
                    ON pc.id = pt.categ_id
                WHERE air.product_id IS NOT NULL
                  AND air.move_type IN ('out_invoice', 'out_refund')
                  AND air.invoice_date >= DATE '2026-01-01'
                GROUP BY
                    date_trunc('month', air.invoice_date)::date,
                    {current_bucket_key},
                    {current_prefix5},
                    air.branch_id,
                    air.team_id,
                    air.invoice_user_id,
                    air.company_id
                HAVING {current_bucket_key} IS NOT NULL
            ),
            current_line_extras AS (
                SELECT
                    date_trunc('month', COALESCE(am.invoice_date, am.date))::date AS period_month,
                    {current_bucket_key} AS bucket_key,
                    {current_prefix5} AS bucket_code_prefix5,
                    am.branch_id AS branch_id,
                    am.team_id AS team_id,
                    am.invoice_user_id AS invoice_user_id,
                    am.company_id AS company_id,
                    SUM(
                        COALESCE(aml.price_unit, 0.0) * COALESCE(aml.signed_quantity, 0.0) * (COALESCE(aml.discount, 0.0) / 100.0)
                    ) AS discount_amount,
                    SUM(
                        COALESCE(aml.price_unit, 0.0) * COALESCE(aml.signed_quantity, 0.0)
                    ) AS gross_sales_amount
                FROM account_move_line aml
                JOIN account_move am
                    ON am.id = aml.move_id
                JOIN product_product pp
                    ON pp.id = aml.product_id
                WHERE aml.product_id IS NOT NULL
                  AND COALESCE(aml.display_type, 'product') = 'product'
                  AND am.state = 'posted'
                  AND am.move_type IN ('out_invoice', 'out_refund')
                  AND COALESCE(am.invoice_date, am.date) >= DATE '2026-01-01'
                GROUP BY
                    date_trunc('month', COALESCE(am.invoice_date, am.date))::date,
                    {current_bucket_key},
                    {current_prefix5},
                    am.branch_id,
                    am.team_id,
                    am.invoice_user_id,
                    am.company_id
                HAVING {current_bucket_key} IS NOT NULL
            ),
            current_sales AS (
                SELECT
                    COALESCE(crs.period_month, cle.period_month) AS period_month,
                    'current'::text AS source_system,
                    COALESCE(crs.bucket_key, cle.bucket_key) AS bucket_key,
                    crs.bucket_name,
                    COALESCE(crs.bucket_code_prefix5, cle.bucket_code_prefix5) AS bucket_code_prefix5,
                    crs.sample_item_code,
                    crs.source_category_name,
                    crs.source_brand_name,
                    COALESCE(crs.branch_id, cle.branch_id) AS branch_id,
                    COALESCE(crs.team_id, cle.team_id) AS team_id,
                    COALESCE(crs.invoice_user_id, cle.invoice_user_id) AS invoice_user_id,
                    COALESCE(crs.company_id, cle.company_id) AS company_id,
                    COALESCE(crs.product_count, 0) AS product_count,
                    COALESCE(crs.sales_qty, 0.0) AS sales_qty,
                    COALESCE(crs.sales_amount, 0.0) AS sales_amount,
                    COALESCE(crs.return_qty, 0.0) AS return_qty,
                    COALESCE(crs.return_amount, 0.0) AS return_amount,
                    COALESCE(cle.discount_amount, 0.0) AS discount_amount,
                    COALESCE(cle.gross_sales_amount, 0.0) AS gross_sales_amount,
                    CASE
                        WHEN COALESCE(crs.sales_qty, 0.0) = 0 THEN NULL
                        ELSE COALESCE(crs.sales_amount, 0.0) / COALESCE(crs.sales_qty, 0.0)
                    END AS asp,
                    crs.cogs_amount,
                    crs.margin_amount,
                    CASE
                        WHEN COALESCE(crs.sales_amount, 0.0) = 0 OR crs.margin_amount IS NULL THEN NULL
                        ELSE (crs.margin_amount / crs.sales_amount) * 100.0
                    END AS margin_pct,
                    COALESCE(crs.cost_available, FALSE) AS cost_available
                FROM current_report_sales crs
                FULL OUTER JOIN current_line_extras cle
                    ON cle.period_month = crs.period_month
                   AND cle.bucket_key = crs.bucket_key
                   AND COALESCE(cle.branch_id, 0) = COALESCE(crs.branch_id, 0)
                   AND COALESCE(cle.team_id, 0) = COALESCE(crs.team_id, 0)
                   AND COALESCE(cle.invoice_user_id, 0) = COALESCE(crs.invoice_user_id, 0)
                   AND COALESCE(cle.company_id, 0) = COALESCE(crs.company_id, 0)
            ),
            combined AS (
                SELECT
                    lsd.source_db,
                    ls.period_month,
                    ls.source_system,
                    ls.bucket_name,
                    ls.bucket_key,
                    ls.bucket_code_prefix5,
                    ls.sample_item_code,
                    ls.source_category_name,
                    ls.source_brand_name,
                    ls.branch_id,
                    ls.team_id,
                    ls.invoice_user_id,
                    ls.company_id,
                    ls.product_count,
                    ls.sales_qty,
                    ls.sales_amount,
                    ls.return_qty,
                    ls.return_amount,
                    ls.discount_amount,
                    ls.gross_sales_amount,
                    ls.asp,
                    ls.cogs_amount,
                    ls.margin_amount,
                    ls.margin_pct,
                    COALESCE(ls.cost_available, FALSE) AS cost_available
                FROM legacy_sales ls
                CROSS JOIN legacy_source_db lsd
                UNION ALL
                SELECT
                    lsd.source_db,
                    cs.period_month,
                    cs.source_system,
                    cs.bucket_name,
                    cs.bucket_key,
                    cs.bucket_code_prefix5,
                    cs.sample_item_code,
                    cs.source_category_name,
                    cs.source_brand_name,
                    cs.branch_id,
                    cs.team_id,
                    cs.invoice_user_id,
                    cs.company_id,
                    cs.product_count,
                    cs.sales_qty,
                    cs.sales_amount,
                    cs.return_qty,
                    cs.return_amount,
                    cs.discount_amount,
                    cs.gross_sales_amount,
                    cs.asp,
                    cs.cogs_amount,
                    cs.margin_amount,
                    cs.margin_pct,
                    COALESCE(cs.cost_available, FALSE) AS cost_available
                FROM current_sales cs
                CROSS JOIN legacy_source_db lsd
            )
            SELECT
                ROW_NUMBER() OVER (
                    ORDER BY
                        period_month,
                        source_system,
                        bucket_code_prefix5 NULLS LAST,
                        bucket_name NULLS LAST,
                        sample_item_code NULLS LAST
                ) AS id,
                source_db,
                period_month,
                source_system,
                bucket_name,
                bucket_key,
                bucket_code_prefix5,
                sample_item_code,
                source_category_name,
                source_brand_name,
                branch_id,
                team_id,
                invoice_user_id,
                company_id,
                product_count,
                sales_qty,
                sales_amount,
                return_qty,
                return_amount,
                discount_amount,
                gross_sales_amount,
                asp,
                cogs_amount,
                margin_amount,
                margin_pct,
                cost_available,
                cost_available AS margin_comparable
            FROM combined
            WHERE bucket_key IS NOT NULL
            """
        )

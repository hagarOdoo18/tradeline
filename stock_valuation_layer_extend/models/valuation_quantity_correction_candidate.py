# -*- coding: utf-8 -*-
from odoo import fields, models, tools


class StockValuationQuantityCorrectionCandidate(models.Model):
    _name = 'stock.valuation.quantity.correction.candidate'
    _description = 'Valuation Quantity Correction Candidate'
    _auto = False
    _rec_name = 'lot_id'
    _order = 'source_date desc, id desc'

    source_svl_id = fields.Many2one(
        'stock.valuation.layer',
        string='Erroneous Valuation Layer',
        readonly=True,
    )
    source_date = fields.Datetime(string='Source Date', readonly=True)
    reference = fields.Char(readonly=True)
    company_id = fields.Many2one('res.company', readonly=True)
    currency_id = fields.Many2one('res.currency', readonly=True)
    product_id = fields.Many2one('product.product', readonly=True)
    item_code = fields.Char(readonly=True)
    lot_id = fields.Many2one('stock.lot', string='Lot/Serial Number', readonly=True)
    physical_qty = fields.Float(
        string='Physical Quantity',
        digits='Product Unit of Measure',
        readonly=True,
    )
    valuation_qty = fields.Float(
        string='Valuation Quantity',
        digits='Product Unit of Measure',
        readonly=True,
    )
    valuation_value = fields.Monetary(
        string='Valuation Value',
        currency_field='currency_id',
        readonly=True,
    )
    correction_qty = fields.Float(
        string='Correction Quantity',
        digits='Product Unit of Measure',
        readonly=True,
    )
    correction_value = fields.Monetary(
        string='Correction Value',
        currency_field='currency_id',
        readonly=True,
    )

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute(f"""
            CREATE OR REPLACE VIEW {self._table} AS (
                WITH valuation_totals AS (
                    SELECT
                        company_id,
                        product_id,
                        lot_id,
                        SUM(quantity) AS valuation_qty,
                        SUM(value) AS valuation_value
                    FROM stock_valuation_layer
                    WHERE lot_id IS NOT NULL
                    GROUP BY company_id, product_id, lot_id
                ),
                physical_totals AS (
                    SELECT
                        sq.company_id,
                        sq.product_id,
                        sq.lot_id,
                        SUM(sq.quantity) AS physical_qty
                    FROM stock_quant sq
                    JOIN stock_location location ON location.id = sq.location_id
                    WHERE sq.lot_id IS NOT NULL
                      AND location.usage = 'internal'
                    GROUP BY sq.company_id, sq.product_id, sq.lot_id
                ),
                quantity_updates AS (
                    SELECT
                        svl.company_id,
                        svl.product_id,
                        svl.lot_id,
                        MIN(svl.id) AS source_svl_id,
                        COUNT(*) AS source_count
                    FROM stock_valuation_layer svl
                    JOIN stock_move move ON move.id = svl.stock_move_id
                    WHERE svl.lot_id IS NOT NULL
                      AND move.reference = 'Product Quantity Updated'
                      AND ABS(svl.quantity + 1.0) < 0.000001
                      AND NOT COALESCE(svl.is_quantity_neutralization, FALSE)
                    GROUP BY svl.company_id, svl.product_id, svl.lot_id
                )
                SELECT
                    source.id AS id,
                    source.id AS source_svl_id,
                    source.create_date AS source_date,
                    move.reference AS reference,
                    source.company_id AS company_id,
                    company.currency_id AS currency_id,
                    source.product_id AS product_id,
                    product.barcode AS item_code,
                    source.lot_id AS lot_id,
                    COALESCE(physical.physical_qty, 0.0) AS physical_qty,
                    totals.valuation_qty AS valuation_qty,
                    totals.valuation_value AS valuation_value,
                    -totals.valuation_qty AS correction_qty,
                    -totals.valuation_value AS correction_value
                FROM quantity_updates updates
                JOIN stock_valuation_layer source ON source.id = updates.source_svl_id
                JOIN stock_move move ON move.id = source.stock_move_id
                JOIN valuation_totals totals
                  ON totals.company_id = source.company_id
                 AND totals.product_id = source.product_id
                 AND totals.lot_id = source.lot_id
                LEFT JOIN physical_totals physical
                  ON physical.company_id = source.company_id
                 AND physical.product_id = source.product_id
                 AND physical.lot_id = source.lot_id
                JOIN res_company company ON company.id = source.company_id
                JOIN product_product product ON product.id = source.product_id
                WHERE updates.source_count = 1
                  AND ABS(COALESCE(physical.physical_qty, 0.0)) < 0.000001
                  AND ABS(totals.valuation_qty + 1.0) < 0.000001
                  AND totals.valuation_value <= 0.000001
                  AND NOT EXISTS (
                      SELECT 1
                      FROM stock_valuation_layer correction
                      WHERE correction.quantity_neutralization_source_id = source.id
                  )
            )
        """)

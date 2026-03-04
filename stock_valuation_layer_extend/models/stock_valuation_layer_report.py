# -*- coding: utf-8 -*-
from odoo import models, fields, tools


class StockValuationLayerReport(models.Model):
    _name = 'stock.valuation.layer.report'
    _description = 'Stock Valuation Layer Report (Grouped by Product)'
    _auto = False
    _rec_name = 'product_id'
    _order = 'product_id'

    # ── Dimensions ─────────────────────────────────────────────────────────────
    product_id        = fields.Many2one('product.product',  string='Product',          readonly=True)
    item_code         = fields.Char(                        string='Item Code',         readonly=True)
    product_categ_id  = fields.Many2one('product.category', string='Category',          readonly=True)
    product_family_id = fields.Many2one('product.family', string='Family',            readonly=True)
    vendor_id         = fields.Many2one('res.partner',      string='Vendor',            readonly=True)
    company_id        = fields.Many2one('res.company',      string='Company',           readonly=True)
    currency_id       = fields.Many2one('res.currency',     string='Currency',          readonly=True)

    # ── Measures ───────────────────────────────────────────────────────────────
    quantity          = fields.Float(  string='Total Quantity',    readonly=True, group_operator='sum')
    value         = fields.Float(  string='Total Value',    readonly=True, group_operator='sum',  digits='Product Price')
    last_po_cost      = fields.Float(  string='Last PO Cost',      readonly=True, group_operator=False, digits='Product Price')
    unit_cost      = fields.Float(  string='Unit Cost',      readonly=True, group_operator=False, digits='Product Price')
    available_qty     = fields.Float(  string='Available Qty',     readonly=True, group_operator=False, digits='Product Unit of Measure')
    layers_count      = fields.Integer(string='# Layers',          readonly=True, group_operator='sum')

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute("""
            CREATE OR REPLACE VIEW stock_valuation_layer_report AS
            SELECT
                -- PK
                MIN(svl.id)                                     AS id,

                -- Dimensions
                svl.product_id                                  AS product_id,
                pp.barcode                                 AS item_code,
                pt.categ_id                                     AS product_categ_id,
                pt.family_id                                    AS product_family_id,
                svl.company_id                                  AS company_id,
                rc.currency_id                                  AS currency_id, 

                pp.vendor_id                                      AS vendor_id,
                pt.standard_price                                  AS unit_cost,

                -- Measures
                COALESCE(SUM(svl.quantity), 0.0)::double precision    AS quantity,
                COALESCE(SUM(svl.value), 0.0)::double precision        AS value,
                COUNT(svl.id)                                   AS layers_count,

                -- Last PO cost per product
                COALESCE((
                    SELECT pol.price_unit
                    FROM purchase_order_line pol
                    JOIN purchase_order po ON po.id = pol.order_id
                    WHERE pol.product_id = svl.product_id
                      AND po.state IN ('purchase', 'done')
                    ORDER BY po.date_approve DESC, pol.id DESC
                    LIMIT 1
                )   ,0.0)::double precision                                            AS last_po_cost,

                -- Available qty (qty_available = sum of quants on hand)
                COALESCE((
                    SELECT SUM(sq.quantity)
                    FROM stock_quant sq
                    JOIN stock_location sl ON sl.id = sq.location_id
                    WHERE sq.product_id = svl.product_id
                      AND sl.usage = 'internal'
                      AND sq.company_id = svl.company_id
                ), 0.0)::double precision                               AS available_qty

            FROM stock_valuation_layer svl
            JOIN product_product pp      ON pp.id  = svl.product_id
            JOIN product_template pt     ON pt.id  = pp.product_tmpl_id
            JOIN product_category pc     ON pc.id  = pt.categ_id
            JOIN res_company rc  ON rc.id = svl.company_id
            GROUP BY
                svl.product_id,
                pp.barcode,
                pt.categ_id,
                pt.family_id,
                svl.company_id,
                rc.currency_id,
                pp.vendor_id,
                pt.standard_price ,
                pt.id
        """)

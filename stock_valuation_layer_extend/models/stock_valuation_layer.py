# -*- coding: utf-8 -*-
from odoo import models, fields, api


class StockValuationLayer(models.Model):
    _inherit = 'stock.valuation.layer'

    # ── Category / Family / Vendor ─────────────────────────────────────────────



    product_family_id = fields.Many2one(
        comodel_name='product.family',
        string='Product Family',
        related='product_id.product_tmpl_id.family_id',
        store=True,
        readonly=True,
    )

    vendor_id = fields.Many2one(
        comodel_name='res.partner',
        string='Vendor',
        related='product_id.vendor_id',
        store=True,
        readonly=True,
    )

    # ── New fields ─────────────────────────────────────────────────────────────

    item_code = fields.Char(
        string='Item Code',
        related='product_id.barcode',
        store=True,
        readonly=True,
    )

    last_po_cost = fields.Float(
        string='Last PO Cost',
        digits='Product Price', group_operator = False,
        compute='_compute_last_po_cost',
        store=True,
        readonly=True,
    )

    available_qty = fields.Float(
        string='Available Qty',
        digits='Product Unit of Measure',
        compute='_compute_available_qty',group_operator = False,
        store=False,
        readonly=True,
    )

    # ── Compute methods ────────────────────────────────────────────────────────

    @api.depends('product_id')
    def _compute_vendor_id(self):
        for rec in self:
            supplier = rec.product_id.seller_ids[:1]
            rec.vendor_id = supplier.partner_id if supplier else False

    @api.depends('product_id')
    def _compute_last_po_cost(self):
        """Most recent confirmed PO line price; fallback to standard_price."""
        PurchaseOrderLine = self.env['purchase.order.line']
        for rec in self:
            if not rec.product_id:
                rec.last_po_cost = 0.0
                continue
            last_line = PurchaseOrderLine.search(
                [
                    ('product_id', '=', rec.product_id.id),
                    ('order_id.state', 'in', ['purchase', 'done']),
                ],
                order='date_approve desc, id desc',
                limit=1,
            )
            rec.last_po_cost = last_line.price_unit if last_line else rec.product_id.standard_price

    def _compute_available_qty(self):
        """Real-time qty on hand."""
        for rec in self:
            rec.available_qty = rec.product_id.qty_available if rec.product_id else 0.0

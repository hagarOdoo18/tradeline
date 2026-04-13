# -*- coding: utf-8 -*-
from odoo import api, fields, models

from .search_helpers import rewrite_product_id_text_domain, search_product_ids_by_text


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

    product_search_text = fields.Char(
        string='Product',
        compute='_compute_product_search_text',
        search='_search_product_search_text',
        store=False,
    )

    last_po_cost = fields.Float(
        string='Last PO Cost',
        digits='Product Price',
        aggregator=False,
        compute='_compute_last_po_cost',
        store=True,
        readonly=True,
    )

    available_qty = fields.Float(
        string='Available Qty',
        digits='Product Unit of Measure',
        compute='_compute_available_qty',
        aggregator=False,
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
    def _compute_product_search_text(self):
        for rec in self:
            rec.product_search_text = rec.product_id.display_name or ''

    def _search_product_search_text(self, operator, value):
        value = (value or '').strip()
        if not value:
            return []
        product_ids = search_product_ids_by_text(self.env, value, operator=operator, limit=5000)
        if not product_ids:
            return [('id', '=', 0)]
        return [('product_id', 'in', product_ids)]

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

    @api.model
    def _rewrite_product_id_text_domain(self, domain):
        return rewrite_product_id_text_domain(self.env, domain)

    @api.model
    def search(self, domain, offset=0, limit=None, order=None):
        domain = self._rewrite_product_id_text_domain(domain)
        return super().search(domain, offset=offset, limit=limit, order=order)

    @api.model
    def read_group(
        self,
        domain,
        fields,
        groupby,
        offset=0,
        limit=None,
        orderby=False,
        lazy=True,
    ):
        domain = self._rewrite_product_id_text_domain(domain)
        return super().read_group(
            domain,
            fields,
            groupby,
            offset=offset,
            limit=limit,
            orderby=orderby,
            lazy=lazy,
        )

# -*- coding: utf-8 -*-
from odoo import api, fields, models


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    is_intercompany_line = fields.Boolean(
        string='Inter-company Tax Applied',
        compute='_compute_is_intercompany_line',
        store=False,
    )

    @api.depends('order_id.partner_id', 'tax_id')
    def _compute_is_intercompany_line(self):
        for line in self:
            partner = line.order_id.partner_id.commercial_partner_id
            line.is_intercompany_line = bool(
                partner
                and partner.is_effective_intercompany()
                and partner.intercompany_tax_ids
            )

    # ── Apply inter-company taxes ─────────────────────────────────────────
    @api.depends(
        'product_id',
        'order_id.partner_id',
        'order_id.fiscal_position_id',
        'order_id.partner_id.intercompany_tax_ids',
        'order_id.partner_id.is_intercompany_customer',
    )
    def _compute_tax_id(self):
        """Replace product taxes with inter-company taxes when applicable."""
        super()._compute_tax_id()
        for line in self:
            partner = line.order_id.partner_id.commercial_partner_id
            if not partner or not partner.is_effective_intercompany():
                continue
            if not partner.intercompany_tax_ids:
                continue
            line.tax_id = partner.intercompany_tax_ids

    # ── Tax-inclusive unit price ──────────────────────────────────────────
    @api.depends(
        'product_id',
        'product_uom',
        'product_uom_qty',
        'order_id.partner_id',
        'order_id.pricelist_id',
        'order_id.date_order',
        'order_id.partner_id.intercompany_price_include',
        'order_id.partner_id.intercompany_tax_ids',
    )
    def _compute_price_unit(self):
        """After Odoo computes the base price, gross it up with inter-company
        taxes when the partner has 'Unit Price Includes Tax' enabled."""
        super()._compute_price_unit()
        for line in self:
            partner = line.order_id.partner_id.commercial_partner_id
            if not partner:
                continue
            if not partner.is_effective_intercompany():
                continue
            if not partner.intercompany_price_include:
                continue
            taxes = partner.intercompany_tax_ids
            if not taxes:
                continue
            # compute_all returns total_included = price + tax amount
            currency = line.order_id.currency_id
            if line.product_id:
                tax_result = taxes.compute_all(
                    line.product_id.lst_price,
                    currency=currency,
                    quantity=1.0,
                    product=line.product_id,
                    partner=line.order_id.partner_id,
                )
                line.price_unit = tax_result['total_included']

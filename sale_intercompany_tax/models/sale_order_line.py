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
        """Compute unit price for inter-company lines.

        Priority:
          1. Pricelist price  — if a pricelist is set on the order and the
                                product has an entry in it.
          2. lst_price        — the product's public sales price as fallback.

        The resulting price is then converted to the order currency when needed.
        """
        super()._compute_price_unit()

        for line in self:
            partner = line.order_id.partner_id.commercial_partner_id
            if not partner or not partner.is_effective_intercompany():
                continue
            if not partner.intercompany_tax_ids or not line.product_id:
                continue

            order      = line.order_id
            pricelist  = order.pricelist_id
            currency   = order.currency_id
            company    = line.company_id or self.env.company
            date       = order.date_order or fields.Date.today()

            # ── 1. Try pricelist price ──────────────────────────────────
            if pricelist:
                pricelist_price = pricelist._get_product_price(
                    line.product_id,
                    line.product_uom_qty or 1.0,
                    currency=currency,
                    date=date,
                    uom=line.product_uom,
                )
                unit_price = pricelist_price
            else:
                # ── 2. Fallback: lst_price converted to order currency ──
                unit_price = line.product_id.lst_price
                if company.currency_id != currency:
                    unit_price = company.currency_id._convert(
                        unit_price,
                        currency,
                        company,
                        date,
                    )

            line.price_unit = unit_price

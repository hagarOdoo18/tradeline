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

    @api.depends(
        'product_id',
        'order_id.partner_id',
        'order_id.fiscal_position_id',
        'order_id.partner_id.intercompany_tax_ids',
        'order_id.partner_id.is_intercompany_customer',
    )
    def _compute_tax_id(self):
        """Apply inter-company taxes when the customer belongs to a different company.

        Calls super() first so standard product/fiscal-position logic runs,
        then replaces the result with the partner's inter-company taxes when applicable.
        """
        super()._compute_tax_id()
        for line in self:
            partner = line.order_id.partner_id.commercial_partner_id
            if not partner:
                continue
            if not partner.is_effective_intercompany():
                continue
            if not partner.intercompany_tax_ids:
                continue
            line.tax_id = partner.intercompany_tax_ids

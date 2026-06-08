# -*- coding: utf-8 -*-
from odoo import api, fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    is_intercompany_customer = fields.Boolean(
        string='Inter-company Customer',
        default=False,
        help='Enable to mark this partner as belonging to a different company. '
             'Sale order lines for this customer will use the taxes defined below '
             'instead of the standard product taxes.',
    )

    intercompany_tax_ids = fields.Many2many(
        comodel_name='account.tax',
        relation='partner_intercompany_tax_rel',
        column1='partner_id',
        column2='tax_id',
        string='Inter-company Taxes',
        domain=[('type_tax_use', 'in', ('sale', 'all'))],
        help='Taxes automatically applied on sale order lines when this '
             'inter-company customer is selected.',
    )

    intercompany_price_include = fields.Boolean(
        string='Unit Price Includes Tax',
        default=False,
        help='When enabled, the unit price on sale order lines for this '
             'inter-company customer will be displayed as tax-inclusive '
             '(gross price = price + inter-company taxes).',
    )

    is_auto_intercompany = fields.Boolean(
        string='Auto-detected Inter-company',
        compute='_compute_is_auto_intercompany',
        store=False,
        help='True when this partner is the commercial partner of another '
             'company registered in this system.',
    )

    @api.depends_context('company')
    def _compute_is_auto_intercompany(self):
        other_partner_ids = set(
            self.env['res.company'].search([
                ('id', '!=', self.env.company.id),
            ]).mapped('partner_id').ids
        )
        for rec in self:
            rec.is_auto_intercompany = rec.id in other_partner_ids

    def is_effective_intercompany(self):
        """Return True if this partner should receive inter-company taxes."""
        self.ensure_one()
        if self.is_intercompany_customer:
            return True
        other_partner_ids = set(
            self.env['res.company'].search([
                ('id', '!=', self.env.company.id),
            ]).mapped('partner_id').ids
        )
        return self.commercial_partner_id.id in other_partner_ids

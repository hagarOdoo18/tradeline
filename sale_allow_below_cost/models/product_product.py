# -*- coding: utf-8 -*-
import datetime as dt
from odoo import api, fields, models
from odoo.exceptions import ValidationError


def _to_date(value):
    """Convert datetime / string / date -> datetime.date, or None."""
    if not value:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


class ProductTemplate(models.Model):
    _inherit = 'product.product'

    below_cost_policy = fields.Selection(
        selection=[
            ('global',   'Use Global Setting'),
            ('allowed',  'Always Allow (within dates)'),
            ('blocked',  'Always Block'),
        ],
        string='Sell Below Cost',
        default='blocked',
    )

    below_cost_date_from = fields.Date(
        string='Below-Cost Allowed From',
    )
    below_cost_date_to = fields.Date(
        string='Below-Cost Allowed To',
    )

    below_cost_action = fields.Selection(
        selection=[
            ('global',   'Use Global Setting'),
            ('warning',  'Show Warning (allow saving)'),
            ('block',    'Block (prevent saving)'),
        ],
        string='Below-Cost Action',
        default='global',
    )

    @api.constrains('below_cost_date_from', 'below_cost_date_to')
    def _check_product_below_cost_dates(self):
        for rec in self:
            # Convert both sides to pure date before comparing
            d_from = _to_date(rec.below_cost_date_from)
            d_to   = _to_date(rec.below_cost_date_to)
            if d_from and d_to and d_from > d_to:
                raise ValidationError(
                    'Product below-cost "Allowed From" must be earlier than or equal to "Allowed To".'
                )



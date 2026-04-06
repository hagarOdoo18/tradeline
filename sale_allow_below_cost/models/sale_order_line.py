# -*- coding: utf-8 -*-
import datetime as dt
from odoo import api, fields, models, _
from odoo.exceptions import UserError


def _get_param(env, key, default=None):
    val = env['ir.config_parameter'].sudo().get_param(key)
    return val if val not in (False, None, '') else default


def _to_date(value):
    """
    Safely convert any of:
      - datetime.date     -> returned as-is
      - datetime.datetime -> .date() extracted
      - str 'YYYY-MM-DD'  -> parsed to datetime.date
      - False / None      -> None
    This prevents the '_work_intervals_batch' AttributeError that occurs when
    Odoo's resource internals receive a date where they expect a datetime.
    """
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


def _date_in_range(date_from, date_to, today):
    """
    Return True if today (datetime.date) falls within [date_from, date_to].
    None on either side means open-ended.
    """
    d_from = _to_date(date_from)
    d_to   = _to_date(date_to)
    if d_from and today < d_from:
        return False
    if d_to and today > d_to:
        return False
    return True


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    # ── Visual helpers ───────────────────────────────────────────────────────
    is_below_cost = fields.Boolean(
        compute='_compute_is_below_cost', store=False)
    below_cost_warning = fields.Char(
        compute='_compute_is_below_cost', store=False)

    @api.depends('price_unit', 'product_id', 'product_uom')
    def _compute_is_below_cost(self):
        for line in self:
            cost = line._get_product_cost()
            if line.price_unit < cost:
                line.is_below_cost = True
                line.below_cost_warning = _(
                    "Price %(price).2f is below cost %(cost).2f for '%(name)s'.",
                    price=line.price_unit,
                    cost=cost,
                    name=line.product_id.display_name,
                )
            else:
                line.is_below_cost = False
                line.below_cost_warning = False

    # ── Cost helper (handles UoM conversion) ─────────────────────────────────
    def _get_product_cost(self):
        self.ensure_one()
        if not self.product_id:
            return 0.0
        cost = self.product_id.standard_price or 0.0
        if (self.product_uom
                and self.product_id.uom_id
                and self.product_uom != self.product_id.uom_id):
            cost = self.product_id.uom_id._compute_price(cost, self.product_uom)
        return cost

    # ── Core policy resolver ─────────────────────────────────────────────────
    def _resolve_below_cost_policy(self):
        """
        Returns (allowed: bool, action: str, reason: str).

        Priority:
          1. Product policy == 'blocked'  → always block
          2. Product policy == 'allowed'  → check product-level date range
          3. Product policy == 'global'   → check global setting + global dates

        All date comparisons use datetime.date objects via _to_date() to avoid
        the "'datetime.date' object has no attribute '_work_intervals_batch'"
        error triggered inside Odoo's resource module.
        """
        env     = self.env
        product = self.product_id
        # Always use dt.date.today() — never datetime.datetime here
        today   = dt.date.today()

        # ── 1. Product hard block ─────────────────────────────────────────────
        if product.below_cost_policy == 'blocked':
            return (
                False,
                'block',
                _("Product '%(name)s' is configured to never be sold below cost.",
                  name=product.display_name),
            )

        # ── 2. Product explicit allow ─────────────────────────────────────────
        if product.below_cost_policy == 'allowed':
            p_from = _to_date(product.below_cost_date_from)
            p_to   = _to_date(product.below_cost_date_to)

            if not _date_in_range(p_from, p_to, today):
                parts = []
                if p_from:
                    parts.append(_("from %s", fields.Date.to_string(p_from)))
                if p_to:
                    parts.append(_("to %s",   fields.Date.to_string(p_to)))
                period = " ".join(parts) if parts else _("the configured period")
                return (
                    False,
                    'block',
                    _("Product '%(name)s': selling below cost is only allowed "
                      "%(period)s (today: %(today)s).",
                      name=product.display_name,
                      period=period,
                      today=fields.Date.to_string(today)),
                )
            else:
                action = _get_param(env, 'sale_allow_below_cost.below_cost_action', 'warning')
                # Product-level action override
                if product.below_cost_action and product.below_cost_action != 'global':
                    action = product.below_cost_action

                return (True, action, '')


        # ── 3. Global setting ─────────────────────────────────────────────────

    # ── Enforcement ──────────────────────────────────────────────────────────
    def _check_below_cost_policy(self):
        errors = []
        for line in self:
            if not line.is_below_cost:
                continue
            allowed, action, reason = line._resolve_below_cost_policy()
            if not allowed or action == 'block':
                errors.append(
                    reason or _(
                        "Price %(price).2f is below cost %(cost).2f for '%(name)s'.",
                        price=line.price_unit,
                        cost=line._get_product_cost(),
                        name=line.product_id.display_name,
                    )
                )
        if errors:
            raise UserError("\n\n".join(errors))

    # ── ORM hooks ────────────────────────────────────────────────────────────
    @api.model_create_multi
    def create(self, vals_list):
        lines = super().create(vals_list)
        lines._check_below_cost_policy()
        return lines

    def write(self, vals):
        res = super().write(vals)
        if 'price_unit' in vals or 'product_id' in vals:
            self._check_below_cost_policy()
        return res


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    def action_confirm(self):
        # Run our check BEFORE super() so resource calendar is never
        # touched with a raw date object from our code.
        for order in self:
            order.order_line._check_below_cost_policy()
        return super().action_confirm()

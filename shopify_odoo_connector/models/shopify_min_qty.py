# -*- coding: utf-8 -*-
"""Minimum quantity rules used when publishing stock on Shopify.

Before this model the buffer was a constant in
``sync.inventory._push_inventory_groups``::

    available = total_qty if total_qty > 3 else 0

meaning: never advertise an item on Shopify unless Odoo holds more than 3
units of it, so the last few units stay for the shops and the store does not
oversell. The threshold is now configurable per warehouse and per product
category (Shopify > Configuration > Minimum Quantities).
"""
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

# Used when no rule matches at all. Keeps the historical behaviour of the
# hard-coded buffer for a database with an empty rule table.
DEFAULT_MIN_QTY = 3
DEFAULT_STRATEGY = 'threshold'


class ShopifyMinQty(models.Model):
    """One minimum-quantity rule.

    A rule is matched on (instance, warehouse, product category). Leaving a
    field empty makes the rule apply to *all* values of it, so a single row
    with everything empty is the global default.

    When several rules match, the most specific one wins, in this order:

    1. instance set beats instance empty;
    2. warehouse set beats warehouse empty;
    3. the closest product category wins - an exact match first, then the
       nearest parent category, then a rule with no category at all.

    Warehouse outranks category on purpose: the threshold exists to protect a
    physical shop's shelf, so what a given warehouse says about a category
    beats what a global category rule says.
    """
    _name = 'shopify.min.qty'
    _description = 'Shopify Minimum Quantity Rule'
    _order = 'instance_id, warehouse_id, categ_id, id'

    instance_id = fields.Many2one(
        'shopify.configuration', string='Shopify Instance',
        ondelete='cascade', index=True,
        help='Leave empty to apply the rule to every Shopify instance.')
    warehouse_id = fields.Many2one(
        'stock.warehouse', string='Warehouse',
        ondelete='cascade', index=True,
        help='Leave empty to apply the rule to every warehouse.')
    categ_id = fields.Many2one(
        'product.category', string='Product Category',
        ondelete='cascade', index=True,
        help='Leave empty to apply the rule to every product category. '
             'A rule set on a category also covers its child categories, '
             'unless a child has a rule of its own.')
    min_qty = fields.Integer(
        string='Minimum Qty', required=True, default=DEFAULT_MIN_QTY,
        help='Quantity kept out of Shopify. See Strategy for how it is used.')
    strategy = fields.Selection(
        selection=[('threshold', 'Hide below minimum'),
                   ('buffer', 'Keep minimum as reserve')],
        string='Strategy', required=True, default=DEFAULT_STRATEGY,
        help='Hide below minimum: publish the full quantity, but publish 0 '
             'while on hand is at or below the minimum.\n'
             'Keep minimum as reserve: always publish on hand minus the '
             'minimum (never below 0).')
    active = fields.Boolean(string='Active', default=True)
    company_id = fields.Many2one(
        'res.company', string='Company',
        related='instance_id.company_id', store=True, readonly=True)

    _sql_constraints = [
        ('min_qty_positive', 'CHECK(min_qty >= 0)',
         'The minimum quantity cannot be negative.'),
    ]

    # ------------------------------------------------------------------
    # display / integrity
    # ------------------------------------------------------------------

    @api.depends('instance_id', 'warehouse_id', 'categ_id', 'min_qty')
    def _compute_display_name(self):
        for rule in self:
            rule.display_name = '%s / %s: %s' % (
                rule.warehouse_id.name or _('All Warehouses'),
                rule.categ_id.complete_name or _('All Categories'),
                rule.min_qty,
            )

    @api.constrains('instance_id', 'warehouse_id', 'categ_id', 'active')
    def _check_unique_rule(self):
        """No two active rules may target the same combination.

        A plain SQL unique index cannot do this: in Postgres NULL is never
        equal to NULL, so it would happily accept ten "all warehouses, all
        categories" rows and the resolution would then depend on the id.
        """
        for rule in self:
            if not rule.active:
                continue
            duplicate = self.search([
                ('id', '!=', rule.id),
                ('instance_id', '=', rule.instance_id.id),
                ('warehouse_id', '=', rule.warehouse_id.id),
                ('categ_id', '=', rule.categ_id.id),
            ], limit=1)
            if duplicate:
                raise ValidationError(_(
                    'There is already a minimum quantity rule for '
                    'instance "%(instance)s", warehouse "%(warehouse)s" and '
                    'category "%(category)s".',
                    instance=rule.instance_id.name or _('All'),
                    warehouse=rule.warehouse_id.name or _('All'),
                    category=rule.categ_id.complete_name or _('All'),
                ))

    # ------------------------------------------------------------------
    # resolution
    # ------------------------------------------------------------------

    @api.model
    def _build_index(self, instance=None):
        """Read the rules once and return a lookup dict.

        The inventory push resolves a minimum for every (location, group)
        pair, so the rules are read once per batch and matched in memory
        instead of one search per group.

        Returns ``{(instance_id, warehouse_id, categ_id): (min_qty,
        strategy)}`` with ``0`` standing for "empty / applies to all".
        """
        domain = []
        if instance:
            domain = ['|', ('instance_id', '=', False),
                      ('instance_id', '=', instance.id)]
        index = {}
        for rule in self.sudo().search_read(
                domain, ['instance_id', 'warehouse_id', 'categ_id',
                         'min_qty', 'strategy']):
            key = (rule['instance_id'] and rule['instance_id'][0] or 0,
                   rule['warehouse_id'] and rule['warehouse_id'][0] or 0,
                   rule['categ_id'] and rule['categ_id'][0] or 0)
            index[key] = (rule['min_qty'],
                          rule['strategy'] or DEFAULT_STRATEGY)
        return index

    @api.model
    def _categ_chain(self, categ_ids):
        """Return ``{categ_id: [self, parent, grand-parent, ...]}``.

        Built from ``parent_path`` so the whole ancestry costs one read.
        """
        categ_ids = [categ_id for categ_id in set(categ_ids or ()) if categ_id]
        if not categ_ids:
            return {}
        chains = {}
        for row in self.env['product.category'].sudo().browse(
                categ_ids).exists().read(['parent_path']):
            path = (row.get('parent_path') or '').strip('/')
            ancestors = [int(part) for part in path.split('/') if part]
            # deepest first: the category itself, then up to the root
            chains[row['id']] = ancestors[::-1] or [row['id']]
        return chains

    @api.model
    def _resolve(self, index, instance_id, warehouse_id, categ_chain):
        """Most specific ``(min_qty, strategy)`` for one combination.

        `categ_chain` is the category and its ancestors, deepest first (see
        :meth:`_categ_chain`).
        """
        if index:
            for inst_key in (instance_id or 0, 0):
                for wh_key in (warehouse_id or 0, 0):
                    for categ_key in list(categ_chain or ()) + [0]:
                        match = index.get((inst_key, wh_key, categ_key))
                        if match is not None:
                            return match
        return DEFAULT_MIN_QTY, DEFAULT_STRATEGY

    @api.model
    def _apply(self, qty, min_qty, strategy):
        """Quantity to publish on Shopify for `qty` units on hand."""
        qty = int(qty or 0)
        min_qty = int(min_qty or 0)
        if strategy == 'buffer':
            return max(qty - min_qty, 0)
        # 'threshold' - historical behaviour: strictly above the minimum
        return qty if qty > min_qty else 0

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

    A rule is matched on (instance, warehouses, product categories). Warehouses
    and categories are many2many, so a single row can cover "Cairo + Alex" and
    "Phones + Tablets" at once. Leaving a list empty makes the rule apply to
    *all* values of it, so a row with everything empty is the global default.

    When several rules match, the most specific one wins, in this order:

    1. instance set beats instance empty;
    2. a listed warehouse beats an empty warehouse list;
    3. the closest product category wins - a listed category first, then the
       nearest listed parent category, then a rule with no category at all.

    Warehouse outranks category on purpose: the threshold exists to protect a
    physical shop's shelf, so what a given warehouse says about a category
    beats what a global category rule says.
    """
    _name = 'shopify.min.qty'
    _description = 'Shopify Minimum Quantity Rule'
    _order = 'instance_id, min_qty desc, id'

    instance_id = fields.Many2one(
        'shopify.configuration', string='Shopify Instance',
        ondelete='cascade', index=True,
        help='Leave empty to apply the rule to every Shopify instance.')
    warehouse_ids = fields.Many2many(
        'stock.warehouse',
        'shopify_min_qty_warehouse_rel', 'min_qty_id', 'warehouse_id',
        string='Warehouses',
        help='Leave empty to apply the rule to every warehouse.')
    categ_ids = fields.Many2many(
        'product.category',
        'shopify_min_qty_categ_rel', 'min_qty_id', 'categ_id',
        string='Product Categories',
        help='Leave empty to apply the rule to every product category. '
             'A listed category also covers its child categories, unless a '
             'child is listed on a rule of its own.')
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

    @api.depends('warehouse_ids', 'categ_ids', 'min_qty')
    def _compute_display_name(self):
        for rule in self:
            warehouses = ', '.join(
                rule.warehouse_ids.mapped('name')) or _('All Warehouses')
            categories = ', '.join(
                rule.categ_ids.mapped('complete_name')) or _('All Categories')
            rule.display_name = '%s / %s: %s' % (
                warehouses, categories, rule.min_qty)

    def _expanded_keys(self):
        """The concrete (instance, warehouse, category) triples this rule
        covers, with ``0`` standing for "empty / applies to all".

        This is the same expansion :meth:`_build_index` does, and it is what
        makes "do two rules overlap?" a plain set intersection.
        """
        self.ensure_one()
        instance_key = self.instance_id.id or 0
        warehouse_keys = self.warehouse_ids.ids or [0]
        categ_keys = self.categ_ids.ids or [0]
        return {(instance_key, warehouse_key, categ_key)
                for warehouse_key in warehouse_keys
                for categ_key in categ_keys}

    @api.constrains('instance_id', 'warehouse_ids', 'categ_ids', 'active')
    def _check_unique_rule(self):
        """No two active rules may cover the same combination.

        Only rules of the *same shape* clash: a global default (no warehouse,
        no category) does not conflict with a rule listing a warehouse,
        because they expand to different keys - the specific one simply wins
        at resolution time. Two rules that both list warehouse "Cairo" and
        both list category "Phones" do clash, and without this check which one
        applied would depend on the record id.

        A SQL unique index could not do this at all: the pairs live in
        many2many tables, and in Postgres NULL is never equal to NULL.
        """
        for rule in self:
            if not rule.active:
                continue
            keys = rule._expanded_keys()
            others = self.search([
                ('id', '!=', rule.id),
                ('instance_id', '=', rule.instance_id.id),
            ])
            for other in others:
                clash = keys & other._expanded_keys()
                if not clash:
                    continue
                _instance_key, warehouse_key, categ_key = sorted(clash)[0]
                raise ValidationError(_(
                    'Rule "%(other)s" already covers warehouse '
                    '"%(warehouse)s" and category "%(category)s" for this '
                    'Shopify instance. Merge the two rules or narrow one of '
                    'them.',
                    other=other.display_name,
                    warehouse=(self.env['stock.warehouse'].browse(
                        warehouse_key).name if warehouse_key
                        else _('All Warehouses')),
                    category=(self.env['product.category'].browse(
                        categ_key).complete_name if categ_key
                        else _('All Categories')),
                ))

    # ------------------------------------------------------------------
    # resolution
    # ------------------------------------------------------------------

    @api.model
    def _build_index(self, instance=None):
        """Read the rules once and return a flat lookup dict.

        Each rule is expanded into one entry per (warehouse, category) pair it
        covers, so resolving a minimum during the inventory push is a dict
        lookup instead of a search per group.

        Returns ``{(instance_id, warehouse_id, categ_id): (min_qty,
        strategy)}`` with ``0`` standing for "empty / applies to all".
        """
        domain = []
        if instance:
            domain = ['|', ('instance_id', '=', False),
                      ('instance_id', '=', instance.id)]
        index = {}
        for rule in self.sudo().search_read(
                domain, ['instance_id', 'warehouse_ids', 'categ_ids',
                         'min_qty', 'strategy']):
            instance_key = rule['instance_id'] and rule['instance_id'][0] or 0
            value = (rule['min_qty'], rule['strategy'] or DEFAULT_STRATEGY)
            for warehouse_key in rule['warehouse_ids'] or [0]:
                for categ_key in rule['categ_ids'] or [0]:
                    key = (instance_key, warehouse_key, categ_key)
                    current = index.get(key)
                    # _check_unique_rule keeps this from happening; should two
                    # rules still collide (a rule written around the
                    # constraint), the stricter one wins rather than the last
                    # one read, so the result never depends on the row order.
                    if current is None or value[0] > current[0]:
                        index[key] = value
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

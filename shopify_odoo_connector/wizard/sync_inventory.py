# -*- coding: utf-8 -*-
################################################################################
#
#    Cybrosys Technologies Pvt. Ltd.
#
#    Copyright (C) 2025-TODAY Cybrosys Technologies(<https://www.cybrosys.com>).
#    Author: Cybrosys Techno Solutions (Contact : odoo@cybrosys.com)
#
#    This program is under the terms of the Odoo Proprietary License v1.0
#    (OPL-1)
#    It is forbidden to publish, distribute, sublicense, or sell copies of the
#    Software or modified copies of the Software.
#
#    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
#    IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
#    DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
#    OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE
#    USE OR OTHER DEALINGS IN THE SOFTWARE.
#
################################################################################
import json
import logging
import re
import requests
from datetime import timedelta
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

# Groups queued per job.cron batch.
INVENTORY_BATCH_SIZE = 20
# Never let the queue grow without bound: if this many inventory jobs are
# already pending for an instance, skip queueing more this run.
MAX_PENDING_JOBS = 100
# Safety overlap when reading "what changed since the last run", to absorb
# clock skew and rows committed just after the previous watermark was taken.
WATERMARK_OVERLAP_MINUTES = 15
# Re-push everything at least this often, so any drift self-heals.
FULL_RESYNC_HOURS = 24
# Shopify page size (the API default is 50 - 250 is the maximum).
SHOPIFY_PAGE_LIMIT = 250
SHOPIFY_TIMEOUT = 30


class SyncInventory(models.TransientModel):
    """Class for transient model Shopify Inventory.
        Methods:
            sync_inventory(self):
                method to sync inventory between shopify and odoo.
    """
    _name = 'sync.inventory'
    _description = 'Sync Inventory'

    import_inventory = fields.Selection(
        string='Import/Export',
        selection=[('shopify', 'To Shopify'), ('odoo', 'From Shopify')],
        required=True, default='shopify',
        help='Field to choose type of data exchange',
    )
    shopify_instance_id = fields.Many2one(
        'shopify.configuration',
        string='Shopify Instance',
        required=True,
        help='Id of shopify instance',
    )
    warehouse_ids = fields.Many2many(
        'stock.warehouse',
        string='Warehouses',
        required=True,
        help='Warehouses to read / update inventory quantities',
    )

    # ------------------------------------------------------------------
    # grouping helpers
    # ------------------------------------------------------------------

    @api.model
    def _inventory_group_key(self, product):
        """Return the code that identifies the physical item `product` belongs
        to.

        The same physical item can exist as several product.product records:
        one "master" variant carrying the code in `barcode`, and one or more
        "alias" variants carrying the very same code in `shopify_variant_sku`.
        Both spellings therefore collapse to a single key, so their stock is
        counted once and pushed to every Shopify variant of the group.

        A variant with neither code gets a key of its own ('id:<id>') so it is
        still pushed, on its own, instead of being merged with unrelated
        codeless variants.
        """
        if not product:
            return ''
        code = ((product.shopify_variant_sku or '').strip()
                or (product.barcode or '').strip())
        return code or 'id:%s' % product.id

    @api.model
    def _build_inventory_groups(self, instance, sync_records=None,
                                products=None):
        """Group variants by barcode / shopify_variant_sku.

        Returns a plain (JSON serialisable) list of dicts, one per physical
        item::

            [{'key': '6291234567890',
              'product_ids': [12, 340, 341],      # odoo variants to sum
              'variant_ids': ['4711', '4712']},   # shopify variants to update
             ...]

        `product_ids` holds every product.product whose `barcode` OR
        `shopify_variant_sku` equals the key — that is the set whose on-hand
        quantities get added together. `variant_ids` holds every Shopify
        variant mapped (through shopify.sync) to any product of the group, so
        the same total is published on all of them.

        The seed can be given as `products` (e.g. the products of a validated
        picking) or as `sync_records`; with neither, every synced variant of
        the instance is used. Either way the group is completed from the
        product table, so a variant that shares the code but was not in the
        seed still contributes its stock and still gets updated.

        Quantities are deliberately NOT computed here: the groups are queued as
        job.cron records and processed one batch per cron tick, so the numbers
        are read at push time to avoid publishing a stale figure.
        """
        if products is None:
            if sync_records is None:
                sync_records = self.env['shopify.sync'].sudo().search([
                    ('instance_id', '=', instance.id),
                    ('shopify_variant_id', '!=', False),
                    ('product_prod_id', '!=', False),
                ])
            products = sync_records.mapped('product_prod_id')
        # archived variants count nowhere: not as a seed, not as a member
        products = products.filtered('active')
        if not products:
            return []

        # 1. one bucket per code, seeded with the given variants
        key_products = {}   # key -> set of product.product ids
        for product in products:
            key = self._inventory_group_key(product)
            key_products.setdefault(key, set()).add(product.id)

        # 2. pull in every other variant sharing the same code, matched on
        #    barcode OR shopify_variant_sku (one search for all codes)
        codes = [key for key in key_products if not key.startswith('id:')]
        if codes:
            # active variants only - an archived variant's stock was never
            # part of the total before and should not start counting now
            members = self.env['product.product'].sudo().search([
                '|',
                ('barcode', 'in', codes),
                ('shopify_variant_sku', 'in', codes),
            ])
            for product in members:
                for code in ((product.barcode or '').strip(),
                             (product.shopify_variant_sku or '').strip()):
                    if code and code in key_products:
                        key_products[code].add(product.id)

        # 3. every Shopify variant mapped to any member of a group
        product_key = {}
        for key, product_ids in key_products.items():
            for product_id in product_ids:
                product_key.setdefault(product_id, key)

        key_variants = {}
        if product_key:
            for sync in self.env['shopify.sync'].sudo().search([
                ('instance_id', '=', instance.id),
                ('shopify_variant_id', '!=', False),
                ('product_prod_id', 'in', list(product_key)),
            ]):
                key = product_key.get(sync.product_prod_id.id)
                if key:
                    key_variants.setdefault(key, set()).add(
                        str(sync.shopify_variant_id))

        # groups with no Shopify variant have nothing to push
        return [{
            'key': key,
            'product_ids': sorted(product_ids),
            'variant_ids': sorted(key_variants[key]),
        } for key, product_ids in key_products.items() if key_variants.get(key)]

    # ------------------------------------------------------------------
    # quantity helpers
    # ------------------------------------------------------------------

    def _get_qty_by_product(self, product_ids, company_id, location):
        """Sellable quantity per product at `location`, in ONE query.

        Returns {product_id: qty}. `quantity` is on hand; what is already
        reserved for other outgoing moves is subtracted so the same units are
        not offered twice on Shopify. Products with no quant are absent from
        the mapping (they count as zero).

        This is read once per location for a whole batch, instead of one
        stock.quant search per group per location.
        """
        if not product_ids or not location:
            return {}

        groups = self.env['stock.quant'].sudo()._read_group(
            [('product_id', 'in', list(product_ids)),
             ('company_id', '=', company_id),
             ('location_id', '=', location.id)],
            groupby=['product_id'],
            aggregates=['quantity:sum', 'reserved_quantity:sum'],
        )
        return {product.id: (quantity or 0.0) - (reserved or 0.0)
                for product, quantity, reserved in groups}

    def _get_group_qty(self, product_ids, company_id, location,
                       qty_by_product=None):
        """Return the sellable quantity of a whole group at `location`.

        `product_ids` are the product.product records that share one
        barcode / shopify_variant_sku code; their quantities are added
        together. Pass `qty_by_product` (from :meth:`_get_qty_by_product`) to
        reuse a mapping already read for the whole batch.
        """
        if not product_ids or not location:
            return 0.0
        if qty_by_product is None:
            qty_by_product = self._get_qty_by_product(
                product_ids, company_id, location)
        total = sum(qty_by_product.get(product_id, 0.0)
                    for product_id in product_ids)
        return max(total, 0.0)

    def _get_odoo_qty(self, product, company_id, location):
        """Sellable quantity of `product` (plus its alias variants) at
        `location`. Kept for backward compatibility - it simply resolves the
        product's group and delegates to :meth:`_get_group_qty`."""
        if not product or not location:
            return 0.0

        product_ids = list(product.ids)
        # Only look for alias variants when the product actually has a barcode.
        # With an empty barcode the domain degrades to
        # ('shopify_variant_sku', '=', False), which matches every product that
        # has no Shopify SKU set and pulls unrelated stock into the total.
        if product.barcode:
            product_ids += self.env['product.product'].sudo().search([
                ('shopify_variant_sku', '=', product.barcode),
                ('id', 'not in', product_ids),
            ]).ids

        return self._get_group_qty(product_ids, company_id, location)

    def _apply_inventory_for_warehouse(self, warehouse, product, qty, company_id):
        """Create or update a stock.quant for one warehouse."""
        exist = self.env['stock.quant'].sudo().search([
            ('location_id', '=', warehouse.lot_stock_id.id),
            ('product_id',  '=', product.id),
            ('lot_id',      '=', False),
            ('company_id',  '=', company_id),
        ])
        if exist:
            exist.sudo().action_set_inventory_quantity()
            exist.inventory_quantity = exist.inventory_quantity + qty
            exist.sudo().action_apply_inventory()
        else:
            (self.env['stock.quant']
             .with_context(inventory_mode=True)
             .create({
                 'product_id':         product.id,
                 'inventory_quantity': qty,
                 'location_id':        warehouse.lot_stock_id.id,
             })
             .action_apply_inventory())

    # ------------------------------------------------------------------
    # shopify variant -> inventory item mapping
    # ------------------------------------------------------------------

    @api.model
    def _inventory_item_param_key(self, instance):
        return 'shopify_odoo_connector.inventory_items.%s' % instance.id

    @api.model
    def _read_inventory_item_cache(self, instance):
        param = self.env['ir.config_parameter'].sudo()
        try:
            cache = json.loads(
                param.get_param(self._inventory_item_param_key(instance))
                or '{}')
        except ValueError:
            cache = {}
        return (cache.get('items') or {},
                set(cache.get('misses') or []))

    @api.model
    def _get_inventory_item_map(self, instance, variant_ids):
        """Return {shopify variant id: inventory_item_id} for `variant_ids`.

        The mapping is cached in ir.config_parameter because inventory item
        ids never change for a variant. Only variants missing from the cache
        trigger a call to Shopify, so a steady-state run makes no request at
        all. Previously every queued batch re-downloaded the whole catalogue
        just to rebuild this map.

        Variants that a fresh crawl could not resolve (deleted on Shopify but
        still carrying a shopify.sync row) are remembered as misses, otherwise
        a single dangling variant would force a full crawl on every run. The
        miss list is cleared by the periodic full resync.
        """
        param = self.env['ir.config_parameter'].sudo()
        items, misses = self._read_inventory_item_cache(instance)

        wanted = {str(variant_id) for variant_id in variant_ids}
        if wanted - set(items) - misses:
            fetched = self._fetch_inventory_item_map(instance)
            if fetched:
                items.update(fetched)
                # still unresolved after a fresh crawl => really not on
                # Shopify. Only trust this when the crawl returned something:
                # an empty result is not evidence that a variant is gone.
                misses |= (wanted - set(items))
                param.set_param(
                    self._inventory_item_param_key(instance),
                    json.dumps({'items': items, 'misses': sorted(misses)}))

        return {variant_id: items[variant_id]
                for variant_id in wanted if variant_id in items}

    @api.model
    def _clear_inventory_item_misses(self, instance):
        """Forget the "not on Shopify" list so re-created variants are picked
        up again. Called by the periodic full resync."""
        items, misses = self._read_inventory_item_cache(instance)
        if misses:
            self.env['ir.config_parameter'].sudo().set_param(
                self._inventory_item_param_key(instance),
                json.dumps({'items': items, 'misses': []}))

    def _fetch_inventory_item_map(self, instance):
        """Crawl the Shopify catalogue once and return the full variant map."""
        products = self._fetch_all_shopify_products(
            instance.shop_name, instance.version,
            instance._get_shopify_headers(), fields_param='id,variants')
        return {str(variant['id']): variant['inventory_item_id']
                for product in products
                for variant in product.get('variants', [])
                if variant.get('inventory_item_id')}

    def _fetch_all_shopify_products(self, store_name, version, headers,
                                    fields_param=None):
        """Fetch all products from Shopify (handles pagination).

        `fields_param` restricts the returned columns (e.g. 'id,variants') to
        keep the payload small when only the variant map is needed.
        """
        base_url = "https://%s/admin/api/%s/products.json" % (
            store_name, version)
        query = ['limit=%s' % SHOPIFY_PAGE_LIMIT]
        if fields_param:
            query.append('fields=%s' % fields_param)
        url = base_url + '?' + '&'.join(query)
        payload = []
        response = requests.request("GET", url, headers=headers, data=payload,
                                    timeout=SHOPIFY_TIMEOUT)
        # A 401/429/5xx still returns a JSON body, so without this an errored
        # crawl looks exactly like an empty catalogue - and the caller would
        # cache "this variant does not exist on Shopify" for every variant.
        response.raise_for_status()
        products = response.json().get('products', [])

        inventory_link = response.headers.get('link', '')
        inventory_links = inventory_link.split(',')
        for link in inventory_links:
            if re.compile(r'rel=\"next\"').search(link):
                inventory_link = link

        rel = (re.search('rel=\"(.*)\"', inventory_link).group(1)
               if 'link' in response.headers else '')

        if inventory_link and rel == 'next':
            item, rec = 0, 1
            while item < rec:
                page_info = re.search('page_info=(.*)>', inventory_link).group(1)
                limit = re.search('limit=(.*)&', inventory_link).group(1)
                next_url = ("https://%s/admin/api/%s/products.json"
                            "?limit=%s&page_info=%s") % (
                    store_name, version, limit, page_info)
                if fields_param:
                    next_url += '&fields=%s' % fields_param
                response = requests.request('GET', next_url,
                                            headers=headers, data=payload,
                                            timeout=SHOPIFY_TIMEOUT)
                # a failed page must not silently truncate the crawl
                response.raise_for_status()
                products += response.json().get('products', [])
                inventory_link = response.headers.get('link', '')
                inventory_links = inventory_link.split(',')
                for link in inventory_links:
                    if re.compile(r'rel=\"next\"').search(link):
                        inventory_link = link
                item += 1
                if inventory_link and re.search(r'rel=\"next\"', inventory_link):
                    rec += 1
        return products

    # ------------------------------------------------------------------
    # To Shopify
    # ------------------------------------------------------------------

    @api.model
    def _cron_sync_inventory_to_shopify(self):
        """Scheduled action to push Odoo on-hand quantities to Shopify for all
        active connected instances.

        The synced variants are first grouped by their barcode /
        shopify_variant_sku code (see :meth:`_build_inventory_groups`), so the
        several Odoo variants that represent one physical item are handled as a
        single unit: their stock is summed once and the total is published on
        every Shopify variant of that group.

        Only what actually moved is queued: the products whose stock.quant
        changed since the previous run (plus variants linked to Shopify since
        then), with a full pass every FULL_RESYNC_HOURS to heal any drift.
        Re-queueing the whole catalogue every 5 minutes produced far more jobs
        than the queue could ever drain.

        The groups are split into batches and queued as job.cron records with
        the function 'export_inventory_to_shopify'; the variant -> inventory
        item mapping is resolved here, once, and travels with the payload so
        no batch has to crawl the Shopify catalogue on its own.
        """
        model = self.env['ir.model'].search(
            [('model', '=', 'sync.inventory')])
        instances = self.env['shopify.configuration'].search(
                    [('company_id', '=', self.env.company.id)])
        param = self.env['ir.config_parameter'].sudo()
        for instance in instances:
            try:
                warehouses = self.env['shopify.location'].sudo().search([
                    ('instance_id', '=', instance.id),
                    ('warehouse_id', '!=', False),
                    ('active', '=', True),
                ]).mapped('warehouse_id')
                if not warehouses:
                    continue

                # Do not pile onto a queue that is still being worked off.
                pending = self.env['job.cron'].sudo().search_count([
                    ('state', '=', 'pending'),
                    ('function', '=', 'export_inventory_to_shopify'),
                    ('instance_id', '=', instance.id),
                ])
                if pending >= MAX_PENDING_JOBS:
                    _logger.info(
                        'Shopify inventory sync: %d job(s) still pending for '
                        'instance %s, skipping this run', pending,
                        instance.name)
                    continue

                run_start = fields.Datetime.now()
                mark_key = ('shopify_odoo_connector.inventory_watermark.%s'
                            % instance.id)
                full_key = mark_key + '.full'
                last_run = param.get_param(mark_key)
                last_full = param.get_param(full_key)
                full_run = not last_run or not last_full or (
                    run_start - fields.Datetime.to_datetime(last_full)
                    > timedelta(hours=FULL_RESYNC_HOURS))

                if full_run:
                    products = None     # every synced variant
                    self._clear_inventory_item_misses(instance)
                else:
                    products = self._changed_products_since(
                        instance, warehouses,
                        fields.Datetime.to_datetime(last_run)
                        - timedelta(minutes=WATERMARK_OVERLAP_MINUTES))
                    if not products:
                        param.set_param(mark_key, fields.Datetime.to_string(run_start))
                        continue

                groups = self._build_inventory_groups(
                    instance, products=products)
                if not groups:
                    param.set_param(mark_key, fields.Datetime.to_string(run_start))
                    if full_run:
                        param.set_param(full_key, fields.Datetime.to_string(run_start))
                    continue

                # Resolve the inventory item ids once for the whole run.
                item_map = self._get_inventory_item_map(
                    instance,
                    {variant_id for group in groups
                     for variant_id in group['variant_ids']})
                for group in groups:
                    group['items'] = {
                        variant_id: item_map[variant_id]
                        for variant_id in group['variant_ids']
                        if variant_id in item_map
                    }
                groups = [group for group in groups if group['items']]
                if not groups:
                    # nothing here maps to a live Shopify variant; advance the
                    # full marker too, otherwise every tick would re-run as a
                    # full pass and re-crawl the catalogue
                    param.set_param(
                        mark_key, fields.Datetime.to_string(run_start))
                    if full_run:
                        param.set_param(
                            full_key, fields.Datetime.to_string(run_start))
                    continue

                warehouse_ids = warehouses.ids
                size = INVENTORY_BATCH_SIZE
                for i in range(0, len(groups), size):
                    self.env['job.cron'].sudo().create([{
                        'model_id': model.id,
                        'function': 'export_inventory_to_shopify',
                        'data': {
                            'groups': groups[i:i + size],
                            'warehouse_ids': warehouse_ids,
                        },
                        'instance_id': instance.id,
                    }])

                param.set_param(mark_key, fields.Datetime.to_string(run_start))
                if full_run:
                    param.set_param(full_key, fields.Datetime.to_string(run_start))
                _logger.info(
                    'Shopify inventory sync (%s): queued %d group(s) covering '
                    '%d variant(s) in %d batch(es) for instance %s',
                    'full' if full_run else 'incremental',
                    len(groups),
                    sum(len(group['items']) for group in groups),
                    (len(groups) + size - 1) // size,
                    instance.name)
            except Exception as error:
                _logger.exception(
                    'Failed to queue inventory push to Shopify for instance '
                    '%s: %s', instance.name, str(error))

    @api.model
    def _changed_products_since(self, instance, warehouses, since):
        """Products whose sellable stock may have moved since `since`.

        That is every product with a stock.quant touched since then in one of
        the mapped stock locations (a quant is written on both a quantity and
        a reservation change), plus every product moved in or out of those
        locations, plus every product whose Shopify link changed.
        """
        location_ids = warehouses.mapped('lot_stock_id').ids
        if not location_ids:
            return self.env['product.product']

        quant_groups = self.env['stock.quant'].sudo()._read_group(
            [('company_id', '=', instance.company_id.id),
             ('location_id', 'in', location_ids),
             ('write_date', '>=', since)],
            groupby=['product_id'],
        )
        product_ids = {product.id for (product,) in quant_groups}

        # A quant that reaches zero can be removed by Odoo's quant clean-up,
        # leaving no row with a recent write_date - the drop to zero would go
        # unnoticed. The completed moves still carry the evidence.
        move_groups = self.env['stock.move.line'].sudo()._read_group(
            [('state', '=', 'done'),
             ('write_date', '>=', since),
             '|',
             ('location_id', 'in', location_ids),
             ('location_dest_id', 'in', location_ids)],
            groupby=['product_id'],
        )
        product_ids |= {product.id for (product,) in move_groups}

        # write_date, not create_date: a sync row that only later receives its
        # shopify_variant_id has to be picked up too.
        new_syncs = self.env['shopify.sync'].sudo().search([
            ('instance_id', '=', instance.id),
            ('shopify_variant_id', '!=', False),
            ('product_prod_id', '!=', False),
            ('write_date', '>=', since),
        ])
        product_ids |= set(new_syncs.mapped('product_prod_id').ids)

        return self.env['product.product'].sudo().browse(
            sorted(product_ids)).exists()

    @api.model
    def export_inventory_to_shopify(self, data, instance):
        """Process a single queued inventory batch (called by job.cron._do_job).

        `data` is the Json payload stored on the job.cron record: the `groups`
        of this batch and the `warehouse_ids` to read on-hand quantities from.
        A transient sync.inventory record is created and _sync_to_shopify is
        called for just this batch.

        Jobs queued by an older version of this module carry `sync_ids`
        instead of `groups`; those are still handled, the groups are simply
        rebuilt from the sync records.
        """
        warehouse_ids = data.get('warehouse_ids', [])
        wizard = self.sudo().create({
            'import_inventory': 'shopify',
            'shopify_instance_id': instance.id,
            'warehouse_ids': [(6, 0, warehouse_ids)],
        })
        groups = data.get('groups')
        if groups is None:
            # legacy payload
            sync_records = self.env['shopify.sync'].sudo().browse(
                data.get('sync_ids', [])).exists()
            wizard._sync_to_shopify(sync_records=sync_records)
        else:
            wizard._sync_to_shopify(groups=groups)

    def _sync_to_shopify(self, groups=None, sync_records=None):
        """Push Odoo on-hand quantities to Shopify inventory levels.

        `groups` (batch mode via job.cron) is the pre-built list of groups to
        push. `sync_records` restricts a freshly built grouping to those sync
        records (legacy batch payloads). With neither, every synced variant of
        the instance is grouped and pushed (manual wizard mode).
        """
        shopify_instance = self.shopify_instance_id

        # 1. Get Shopify locations mapped to the selected warehouses
        shopify_locations = self.env['shopify.location'].sudo().search([
            ('instance_id', '=', shopify_instance.id),
            ('warehouse_id', 'in', self.warehouse_ids.ids),
            ('active', '=', True),
        ]).filtered(lambda loc: loc.shopify_location_id)

        if not shopify_locations:
            raise ValidationError(_(
                'No Shopify location is mapped to the selected warehouse(s). '
                'Please map the selected warehouse(s) to a Shopify location '
                'first (use the Sync Locations wizard).'))

        # 2. Get the groups to push (batch subset or all)
        if groups is None:
            groups = self._build_inventory_groups(
                shopify_instance, sync_records=sync_records)

        self._push_inventory_groups(groups, shopify_locations)

    def _push_inventory_groups(self, groups, shopify_locations):
        """Publish the summed quantity of each group on Shopify.

        For every group the on-hand stock of all its Odoo variants is added up
        per Shopify location, then that single total is written to every
        Shopify variant of the group.
        """
        if not groups:
            return

        shopify_instance = self.shopify_instance_id
        store_name = shopify_instance.shop_name
        version    = shopify_instance.version
        headers    = shopify_instance._get_shopify_headers()
        company_id = shopify_instance.company_id.id

        # variant_id -> inventory_item_id. The cron resolves this once per run
        # and ships it in the payload; only a manual wizard run has to look it
        # up here.
        variant_to_inv_item = {}
        for group in groups:
            variant_to_inv_item.update(group.get('items') or {})
        unmapped = {str(variant_id)
                    for group in groups
                    for variant_id in (group.get('variant_ids') or [])
                    if str(variant_id) not in variant_to_inv_item}
        if unmapped:
            variant_to_inv_item.update(
                self._get_inventory_item_map(shopify_instance, unmapped))

        set_url = ("https://%s/admin/api/%s/inventory_levels/set.json"
                   % (store_name, version))

        # every product touched by this batch, so quantities can be read with
        # one query per location instead of one per group per location
        all_product_ids = sorted({
            product_id
            for group in groups
            for product_id in (group.get('product_ids') or [])
        })

        failures = []
        pushed = 0
        # one TCP/TLS connection for the whole batch instead of one per request
        session = requests.Session()
        try:
            for location in shopify_locations:
                qty_by_product = self._get_qty_by_product(
                    all_product_ids, company_id,
                    location.warehouse_id.lot_stock_id)

                for group in groups:
                    product_ids = group.get('product_ids') or []
                    variant_ids = group.get('variant_ids') or []
                    if not product_ids or not variant_ids:
                        continue

                    total_qty = int(self._get_group_qty(
                        product_ids, company_id,
                        location.warehouse_id.lot_stock_id,
                        qty_by_product=qty_by_product))
                    available = total_qty if total_qty > 3 else 0

                    # the same total goes to every Shopify variant of the group
                    for variant_id in variant_ids:
                        inventory_item_id = variant_to_inv_item.get(
                            str(variant_id))
                        if not inventory_item_id:
                            continue
                        payload = json.dumps({
                            'location_id':       location.shopify_location_id,
                            'inventory_item_id': inventory_item_id,
                            'available':         available,
                        })
                        try:
                            resp = session.post(set_url, headers=headers,
                                                data=payload,
                                                timeout=SHOPIFY_TIMEOUT)
                        except requests.exceptions.RequestException as error:
                            failures.append(
                                'variant %s (group %s, location %s): %s'
                                % (variant_id, group.get('key'),
                                   location.shopify_location_id, error))
                            continue
                        if resp.status_code not in (200, 201):
                            failures.append(
                                'variant %s (group %s, location %s): %s'
                                % (variant_id, group.get('key'),
                                   location.shopify_location_id, resp.text))
                        else:
                            pushed += 1
        finally:
            session.close()

        # One log row per batch instead of one per request (with a commit each
        # one): the per-variant success rows were the bulk of the write load
        # and of the log.message table.
        messages = [{
            'name': 'Inventory push: %d level(s) updated, %d failed '
                    '(%d group(s), %d location(s))'
                    % (pushed, len(failures), len(groups),
                       len(shopify_locations)),
            'shopify_instance_id': shopify_instance.id,
            'model': 'Stock Quantity',
        }]
        if failures:
            messages.append({
                'name': 'Inventory push failures:\n' + '\n'.join(failures[:50]),
                'shopify_instance_id': shopify_instance.id,
                'model': 'Stock Quantity',
            })
        self.env['log.message'].sudo().create(messages)

    # ------------------------------------------------------------------
    # From Shopify
    # ------------------------------------------------------------------

    def _sync_from_shopify(self):
        """Pull Shopify inventory quantities into Odoo stock.quant."""
        shopify_instance = self.shopify_instance_id
        store_name = shopify_instance.shop_name
        version    = shopify_instance.version
        headers    = shopify_instance._get_shopify_headers()
        company_id = shopify_instance.company_id.id

        inventory = self._fetch_all_shopify_products(
            store_name, version, headers)

        for inv in inventory:
            try:
                if inv['options']:
                    for variant in inv['variants']:
                        product = self.env['product.product'].sudo().search([
                            ('shopify_sync_ids.shopify_product', '=',
                             variant['id']),
                            ('shopify_sync_ids.instance_id', '=',
                             shopify_instance.id),
                            ('type',       '=', 'consu'),
                            ('company_id', '=', company_id),
                        ])
                        if product:
                            for warehouse in self.warehouse_ids:
                                self._apply_inventory_for_warehouse(
                                    warehouse, product,
                                    variant['inventory_quantity'],
                                    company_id,
                                )
                else:
                    product = self.env['product.product'].sudo().search([
                        ('shopify_sync_ids.shopify_product', '=', inv['id']),
                        ('shopify_sync_ids.instance_id', '=',
                         shopify_instance.id),
                        ('type',       '=', 'consu'),
                        ('company_id', '=', company_id),
                    ])
                    if product:
                        for warehouse in self.warehouse_ids:
                            self._apply_inventory_for_warehouse(
                                warehouse, product,
                                inv['inventory_quantity'],
                                company_id,
                            )
            except Exception:
                self.env['log.message'].sudo().create([{
                    'name': ('Inventory Syncing not processed for id : '
                             + str(inv['id'])),
                    'shopify_instance_id': shopify_instance.id,
                    'model': 'Stock Quantity',
                }])

    # ------------------------------------------------------------------
    # main action
    # ------------------------------------------------------------------

    def sync_inventory(self):
        """Dispatch to the correct sync direction."""
        if not self.warehouse_ids:
            raise ValidationError(_(
                'Please select at least one warehouse before syncing.'))

        try:
            if self.import_inventory == 'shopify':
                self._sync_to_shopify()
            else:
                self._sync_from_shopify()
        except requests.exceptions.RequestException as error:
            # show the user a message instead of a raw traceback dialog
            raise ValidationError(_(
                'Could not reach Shopify: %s') % error) from error

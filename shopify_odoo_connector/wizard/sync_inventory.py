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
import time
import requests
import urllib3
from datetime import timedelta
from requests.adapters import HTTPAdapter
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

# How many job.cron rows one inventory run may create. The batch size is
# derived from it (groups / INVENTORY_MAX_JOBS), so the number of jobs stays
# flat and only the size of each one grows with the catalogue - the opposite
# of the old fixed batch, where 3000 groups meant 150 jobs.
# Override per database with the system parameter
# 'shopify_odoo_connector.inventory_max_jobs'.
INVENTORY_MAX_JOBS = 5
# Floor for the derived batch size, so a handful of groups is not spread over
# one job each.
INVENTORY_MIN_BATCH_SIZE = 5
# Kept for reference / other callers only. The cron no longer uses a ceiling:
# a run covers the whole catalogue, so it skips while ANY export job is still
# pending rather than waiting for a backlog of this size to build up.
MAX_PENDING_JOBS = 100
# Safety overlap when reading "what changed since the last run", to absorb
# clock skew and rows committed just after the previous watermark was taken.
# Only used by _changed_products_since, which the cron no longer calls.
WATERMARK_OVERLAP_MINUTES = 15
# How often the cached variant -> inventory_item_id "misses" are forgotten,
# so a variant re-created on Shopify is picked up again. The cron queues every
# group on every run, so this no longer decides how much gets pushed.
FULL_RESYNC_HOURS = 24
# Shopify page size (the API default is 50 - 250 is the maximum).
SHOPIFY_PAGE_LIMIT = 250
# GET /inventory_levels.json takes at most 50 inventory_item_ids per call.
INVENTORY_LEVELS_READ_CHUNK = 50
# Quantities per inventorySetQuantities mutation (GraphQL, 2024-07+).
GRAPHQL_SET_CHUNK = 250
# Detail lines per log.message row. The per-group breakdown of a push is
# written as a handful of rows instead of one row per group: the log table
# stays small, and the whole batch is still readable in one place.
INVENTORY_DETAIL_LOG_CHUNK = 100
# First API version with the inventorySetQuantities mutation, which can
# set the *available* quantity of many items in one call. Older versions
# only offer inventorySetOnHandQuantities, which sets on_hand - a
# different number as soon as Shopify has committed stock - so they stay
# on the REST endpoint.
GRAPHQL_INVENTORY_MIN_VERSION = (2024, 7)
# Version the inventory push falls back to when the instance is configured
# with something older than GRAPHQL_INVENTORY_MIN_VERSION.
#
# Shopify supports each version for about a year and "falls forward" for the
# rest: a request naming a retired version is answered by the oldest version
# still accessible. So an instance still set to, say, 2024-01 is already being
# served by a modern version - the old string buys nothing, and dropping to
# one REST POST per item (2 requests/second) over it turns a five-minute push
# into an hour. Naming a supported version here keeps the bulk mutation
# available whatever the instance field says.
#
# Only the inventory push uses this; every other call still uses the
# instance's own version. Raise it when it leaves Shopify's support window.
INVENTORY_API_VERSION = '2025-10'
SHOPIFY_TIMEOUT = 30
# Transient failures to ride out: a dropped keep-alive connection, a gateway
# that could not reach Shopify ("upstream connect error", 502/503/504) and
# Shopify's own 429 throttling. Backoff is 0s, 1s, 2s, 4s between tries, and
# a Retry-After header wins over that.
SHOPIFY_RETRY_TOTAL = 4
SHOPIFY_RETRY_BACKOFF = 1.0
SHOPIFY_RETRY_STATUSES = (429, 500, 502, 503, 504)

# Bulk 'available' write, API version 2024-07 and later. One call carries
# up to GRAPHQL_SET_CHUNK quantities, replacing that many REST POSTs.
GRAPHQL_SET_QUANTITIES = '''
mutation inventorySetQuantities($input: InventorySetQuantitiesInput!) {
  inventorySetQuantities(input: $input) {
    userErrors { field message }
  }
}
'''


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
    update_all_stock = fields.Boolean(
        string='Update All Stock',
        help='Push every synced variant, including the ones whose '
             'quantity Shopify already holds. The work is queued and '
             'processed in the background instead of inside this '
             'request, so a full catalogue can be re-published without '
             'the screen waiting for it.',
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
    # minimum quantity helpers
    # ------------------------------------------------------------------

    def _get_categ_by_product(self, product_ids):
        """Return {product_id: categ_id} for a whole batch in ONE query.

        The minimum quantity is configured per product category, and a push
        batch resolves it for every (location, group) pair - so the category
        of every product is read once here instead of browsing per group.
        """
        if not product_ids:
            return {}
        return {
            row['id']: (row['categ_id'] and row['categ_id'][0] or 0)
            for row in self.env['product.product'].sudo().search_read(
                [('id', 'in', list(product_ids))], ['categ_id'])
        }

    def _get_group_min_qty(self, product_ids, warehouse, index,
                           categ_by_product, categ_chains):
        """Minimum quantity that applies to one inventory group.

        A group can hold variants of different categories (the same physical
        item entered twice under different categories, for instance). The
        strictest rule wins, so the higher minimum is the one applied - never
        publish more than the most protective rule allows.
        """
        instance_id = self.shopify_instance_id.id
        warehouse_id = warehouse.id if warehouse else 0
        rule_model = self.env['shopify.min.qty'].sudo()

        best = None
        for product_id in product_ids:
            categ_id = categ_by_product.get(product_id, 0)
            min_qty, strategy = rule_model._resolve(
                index, instance_id, warehouse_id,
                categ_chains.get(categ_id) or ([categ_id] if categ_id else []))
            if best is None or min_qty > best[0]:
                best = (min_qty, strategy)
        return best or (rule_model._resolve(index, instance_id,
                                            warehouse_id, []))

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
    # http
    # ------------------------------------------------------------------

    @api.model
    def _shopify_session(self):
        """A requests Session that reuses one connection and retries the
        failures worth retrying.

        Without this a single hiccup loses a variant's quantity until the next
        full resync: an idle keep-alive connection dropped by an intermediary
        fails on reuse with no retry at all, and a gateway that momentarily
        cannot reach Shopify answers 502/503 ("upstream connect error or
        disconnect/reset before headers").

        Retrying POST is safe here because every call this session makes is a
        `set` of an absolute quantity, not an increment - replaying it lands on
        the same value.
        """
        retry_kwargs = {
            'total': SHOPIFY_RETRY_TOTAL,
            'connect': SHOPIFY_RETRY_TOTAL,
            'read': 2,
            'status': SHOPIFY_RETRY_TOTAL,
            'backoff_factor': SHOPIFY_RETRY_BACKOFF,
            'status_forcelist': SHOPIFY_RETRY_STATUSES,
            'respect_retry_after_header': True,
            'raise_on_status': False,
        }
        try:
            retry = urllib3.util.retry.Retry(
                allowed_methods=frozenset(['GET', 'POST']), **retry_kwargs)
        except TypeError:
            # urllib3 < 1.26 spells it differently
            retry = urllib3.util.retry.Retry(
                method_whitelist=frozenset(['GET', 'POST']), **retry_kwargs)

        session = requests.Session()
        adapter = HTTPAdapter(max_retries=retry)
        session.mount('https://', adapter)
        session.mount('http://', adapter)
        return session

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
        session = self._shopify_session()
        response = session.get(url, headers=headers, data=payload,
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
                response = session.get(next_url, headers=headers,
                                       data=payload,
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

        Every run queues EVERY group of the instance - the stock.quant /
        stock.move.line watermark is no longer used to narrow the run down to
        what moved. A quantity is therefore re-published on its next tick
        whether or not Odoo shows a change.

        What protects the queue is the pending check: while ANY export job is
        still pending for the instance the run is skipped entirely, so a pass
        always finishes before the next one is queued. The effective cycle is
        "publish everything, wait for it to land, publish everything again" -
        not one pass per cron interval.

        The FULL_RESYNC_HOURS timer is kept, but now only paces the cache
        maintenance (forgetting the "not on Shopify" misses); it no longer
        decides how much is queued.

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

                # Never queue a second pass while the previous one is still
                # being worked off. A run now covers the whole catalogue in
                # INVENTORY_MAX_JOBS jobs, so anything still pending means the
                # last pass has not finished; queueing another would pile up
                # stale batches (the quantities are read at push time, so a
                # batch that waits is a batch that publishes old numbers).
                # The cycle is: push everything -> wait for it to finish ->
                # push everything again.
                pending = self.env['job.cron'].sudo().search_count([
                    ('state', '=', 'pending'),
                    ('function', '=', 'export_inventory_to_shopify'),
                    ('instance_id', '=', instance.id),
                ])
                if pending:
                    _logger.info(
                        'Shopify inventory sync: %d job(s) still pending for '
                        'instance %s, skipping this run', pending,
                        instance.name)
                    continue

                run_start = fields.Datetime.now()
                mark_key = ('shopify_odoo_connector.inventory_watermark.%s'
                            % instance.id)
                full_key = mark_key + '.full'
                last_full = param.get_param(full_key)
                # Only paces the cache maintenance now - not how much is
                # queued. Clearing the miss list forces a catalogue crawl for
                # anything still unresolved, so it stays on its slow cadence.
                full_run = not last_full or (
                    run_start - fields.Datetime.to_datetime(last_full)
                    > timedelta(hours=FULL_RESYNC_HOURS))
                if full_run:
                    self._clear_inventory_item_misses(instance)

                # Every synced variant of the instance, every run: no
                # "what changed since the watermark" filter.
                groups = self._build_inventory_groups(instance, products=None)
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
                # Derived, not fixed: the whole run always fits in at most
                # INVENTORY_MAX_JOBS job.cron rows.
                size = self._inventory_batch_size(len(groups))
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
                    'Shopify inventory sync (all groups%s): queued %d '
                    'group(s) covering %d variant(s) in %d batch(es) for '
                    'instance %s',
                    ', item cache refreshed' if full_run else '',
                    len(groups),
                    sum(len(group['items']) for group in groups),
                    (len(groups) + size - 1) // size,
                    instance.name)
            except Exception as error:
                _logger.exception(
                    'Failed to queue inventory push to Shopify for instance '
                    '%s: %s', instance.name, str(error))

    @api.model
    def _inventory_batch_size(self, total_groups):
        """Groups per job.cron row, so that `total_groups` fit in at most
        INVENTORY_MAX_JOBS jobs.

        The batch size is derived instead of fixed: the queue holds the same
        small number of rows whatever the catalogue size, and one pass is
        something the cron can wait on before queueing the next. Each job
        simply carries more groups as the catalogue grows.

        The ceiling can be tuned per database with the system parameter
        'shopify_odoo_connector.inventory_max_jobs'; a missing or unusable
        value falls back to INVENTORY_MAX_JOBS.
        """
        try:
            max_jobs = int(self.env['ir.config_parameter'].sudo().get_param(
                'shopify_odoo_connector.inventory_max_jobs',
                INVENTORY_MAX_JOBS))
        except (TypeError, ValueError):
            max_jobs = INVENTORY_MAX_JOBS
        max_jobs = max(max_jobs, 1)
        total = max(int(total_groups or 0), 1)
        # ceil division: 3000 groups / 5 jobs -> 600 groups per job
        size = -(-total // max_jobs)
        return max(size, INVENTORY_MIN_BATCH_SIZE)

    @api.model
    def _changed_products_since(self, instance, warehouses, since):
        """Products whose sellable stock may have moved since `since`.

        No longer called by :meth:`_cron_sync_inventory_to_shopify`, which now
        queues every group on every run. Kept because it is the incremental
        filter to put back if the full-catalogue cadence turns out to be too
        heavy for the queue.

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
        # 'force' is set by the Update All Stock action: write every
        # quantity, including the ones Shopify already agrees with.
        force = bool(data.get('force'))
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
            wizard._sync_to_shopify(sync_records=sync_records, force=force)
        else:
            wizard._sync_to_shopify(groups=groups, force=force)

    def _sync_to_shopify(self, groups=None, sync_records=None,
                         force=False):
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

        return self._push_inventory_groups(groups, shopify_locations,
                                           force=force)

    def _push_inventory_groups(self, groups, shopify_locations,
                               force=False):
        """Publish the summed quantity of each group on Shopify.

        For every group the on-hand stock of all its Odoo variants is added up
        per Shopify location, then that single total is written to every
        Shopify variant of the group.

        Every quantity of the batch is always written: the current Shopify
        levels are no longer read and compared, so nothing is ever skipped
        and 'skipped' stays 0. `force` is accepted for backwards
        compatibility with existing callers and payloads but has no effect.

        Besides the summary, a per-group breakdown is written to log.message
        (one line per group and location: on hand, the minimum rule applied,
        the quantity published and the variants it went to), chunked
        INVENTORY_DETAIL_LOG_CHUNK lines to a row.

        Returns {'pushed': n, 'skipped': n, 'failed': n, 'retried': n} so a
        caller that has a user waiting can report what actually happened
        instead of assuming it worked.
        """
        if not groups:
            # same keys as the normal return, so a caller can read
            # result['skipped'] without guarding for the empty batch
            return {'pushed': 0, 'skipped': 0, 'failed': 0, 'retried': 0}

        started = time.monotonic()
        shopify_instance = self.shopify_instance_id
        store_name = shopify_instance.shop_name
        # Not necessarily the instance's own version - see
        # :meth:`_inventory_api_version`.
        version    = self._inventory_api_version(shopify_instance)
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

        # Minimum quantity per warehouse / product category. The rules and the
        # products' categories are read once for the whole batch; resolution
        # for each (location, group) pair then happens in memory.
        rule_model = self.env['shopify.min.qty'].sudo()
        min_qty_index = rule_model._build_index(shopify_instance)
        categ_by_product = self._get_categ_by_product(all_product_ids)
        categ_chains = rule_model._categ_chain(categ_by_product.values())

        failures = []
        # One line per (location, group): what was computed and what was
        # published. Written to log.message at the end of the batch so the
        # quantity each group received can be checked after the fact.
        details = []
        pushed = 0
        skipped = 0
        retried = 0
        use_graphql = self._supports_graphql_inventory(version)
        graphql_url = ('https://%s/admin/api/%s/graphql.json'
                       % (store_name, version))
        # one TCP/TLS connection for the whole batch instead of one per
        # request, and transient gateway/throttle failures are retried
        session = self._shopify_session()
        try:
            for location in shopify_locations:
                qty_by_product = self._get_qty_by_product(
                    all_product_ids, company_id,
                    location.warehouse_id.lot_stock_id)

                # Everything this location should end up with, computed
                # before a single request goes out, so the quantities can
                # be compared against Shopify and sent in bulk.
                desired = {}
                variant_by_item = {}
                for group in groups:
                    product_ids = group.get('product_ids') or []
                    variant_ids = group.get('variant_ids') or []
                    if not product_ids or not variant_ids:
                        continue

                    total_qty = int(self._get_group_qty(
                        product_ids, company_id,
                        location.warehouse_id.lot_stock_id,
                        qty_by_product=qty_by_product))
                    # Stock kept out of Shopify, configured per warehouse and
                    # product category (Shopify > Configuration > Minimum
                    # Quantities). With no rule this falls back to the
                    # historical "more than 3 units" buffer.
                    min_qty, strategy = self._get_group_min_qty(
                        product_ids, location.warehouse_id, min_qty_index,
                        categ_by_product, categ_chains)
                    available = rule_model._apply(total_qty, min_qty, strategy)

                    # the same total goes to every Shopify variant of the group
                    sent_variants = []
                    for variant_id in variant_ids:
                        inventory_item_id = variant_to_inv_item.get(
                            str(variant_id))
                        if not inventory_item_id:
                            continue
                        desired[str(inventory_item_id)] = available
                        sent_variants.append(str(variant_id))
                        variant_by_item.setdefault(
                            str(inventory_item_id), (variant_id,
                                                     group.get('key')))

                    details.append(
                        'group %s | %s (location %s) | on hand %s | '
                        'min %s (%s) | published %s | variant(s): %s'
                        % (group.get('key') or '-',
                           location.warehouse_id.name or '-',
                           location.shopify_location_id,
                           total_qty, min_qty, strategy, available,
                           ', '.join(sent_variants) or 'none (unmapped)'))

                if not desired:
                    continue

                # Always re-publish every quantity of the batch: no read of
                # the current Shopify levels, no comparison, nothing skipped.
                # The `force` argument is kept for callers that still pass it,
                # but it no longer changes what is sent.
                changed = dict(desired)
                if not changed:
                    continue

                if use_graphql:
                    done, leftover, errors = self._graphql_set_levels(
                        session, graphql_url, headers,
                        location.shopify_location_id, changed)
                    pushed += done
                    failures.extend(errors)
                    changed = leftover   # chunks GraphQL could not place

                done, errors, retries = self._rest_set_levels(
                    session, set_url, headers, location.shopify_location_id,
                    changed, variant_by_item)
                pushed += done
                failures.extend(errors)
                retried += retries
        finally:
            session.close()

        # A summary row plus a handful of detail rows per batch - never one
        # row per request (with a commit each one): the per-variant success
        # rows were the bulk of the write load and of the log.message table.
        elapsed = time.monotonic() - started
        summary = ('Inventory push: %d level(s) updated, %d unchanged '
                   '(skipped), %d failed (%d group(s), %d location(s)) '
                   'in %.1fs via %s'
                   % (pushed, skipped, len(failures), len(groups),
                      len(shopify_locations), elapsed,
                      'GraphQL %s' % version if use_graphql
                      else 'REST %s' % version))
        if retried:
            summary += ' - %d request(s) needed a retry' % retried
        _logger.info('%s', summary)
        messages = [{
            'name': summary,
            'shopify_instance_id': shopify_instance.id,
            'model': 'Stock Quantity',
        }]
        # Per-group breakdown: which quantity every group was published with,
        # per location. Chunked so a large batch does not become one huge row.
        for start in range(0, len(details), INVENTORY_DETAIL_LOG_CHUNK):
            chunk = details[start:start + INVENTORY_DETAIL_LOG_CHUNK]
            header = ('Inventory push detail (%d-%d of %d)'
                      % (start + 1, start + len(chunk), len(details)))
            messages.append({
                'name': '<br/>'.join([header] + chunk),
                'shopify_instance_id': shopify_instance.id,
                'model': 'Stock Quantity',
            })
        if failures:
            messages.append({
                'name': 'Inventory push failures:\n' + '\n'.join(failures[:50]),
                'shopify_instance_id': shopify_instance.id,
                'model': 'Stock Quantity',
            })
        self.env['log.message'].sudo().create(messages)
        return {'pushed': pushed, 'skipped': skipped,
                'failed': len(failures), 'retried': retried}

    # ------------------------------------------------------------------
    # Inventory level transport
    # ------------------------------------------------------------------

    @api.model
    def _inventory_api_version(self, instance):
        """API version the inventory push should call.

        The instance's own version when it can run the bulk mutation,
        INVENTORY_API_VERSION otherwise. An instance left on an old version
        would otherwise fall back to one REST POST per inventory item, which
        Shopify serves at two requests a second - minutes of pushing turn into
        an hour, for a version string Shopify no longer honours anyway (it
        answers retired versions with the oldest supported one).
        """
        version = (instance.version or '').strip()
        if self._supports_graphql_inventory(version):
            return version
        _logger.info(
            'Shopify instance %s is configured with API version %r, which has '
            'no bulk inventory mutation; the inventory push uses %s instead. '
            'Set the instance version to %s or newer to use it everywhere.',
            instance.name, version or '(empty)', INVENTORY_API_VERSION,
            INVENTORY_API_VERSION)
        return INVENTORY_API_VERSION

    @api.model
    def _supports_graphql_inventory(self, version):
        """True when this API version can set *available* in bulk.

        `inventorySetQuantities` (name: available) landed in API version
        2024-07. Before that the only bulk mutation is
        `inventorySetOnHandQuantities`, which writes on_hand - not the
        same figure once Shopify has committed stock - so older versions
        keep using the REST endpoint, which does set `available`.
        """
        match = re.match(r'^(\d{4})-(\d{2})', (version or '').strip())
        if not match:
            return False
        return ((int(match.group(1)), int(match.group(2)))
                >= GRAPHQL_INVENTORY_MIN_VERSION)

    def _get_current_levels(self, session, store_name, version, headers,
                            location_id, inventory_item_ids):
        """Current `available` per inventory item at one location.

        Returns ``({inventory_item_id: available}, failures)``. Items the
        read could not cover are simply absent, so they are treated as
        changed and get pushed - a failed read never silently skips a
        quantity.
        """
        levels = {}
        failures = []
        url = ('https://%s/admin/api/%s/inventory_levels.json'
               % (store_name, version))
        item_ids = [str(item) for item in inventory_item_ids]
        for start in range(0, len(item_ids), INVENTORY_LEVELS_READ_CHUNK):
            chunk = item_ids[start:start + INVENTORY_LEVELS_READ_CHUNK]
            params = {
                'inventory_item_ids': ','.join(chunk),
                'location_ids': str(location_id),
                'limit': SHOPIFY_PAGE_LIMIT,
            }
            try:
                resp = session.get(url, headers=headers, params=params,
                                   timeout=SHOPIFY_TIMEOUT)
            except requests.exceptions.RequestException as error:
                _logger.warning(
                    'Inventory level read failed (location %s): %s - those '
                    'items will be pushed unconditionally',
                    location_id, error)
                continue
            if resp.status_code != 200:
                _logger.warning(
                    'Inventory level read failed (location %s): HTTP %s %s',
                    location_id, resp.status_code,
                    (resp.text or '').strip()[:200])
                continue
            try:
                body = resp.json() or {}
            except ValueError:
                continue
            for level in body.get('inventory_levels') or []:
                if level.get('available') is None:
                    continue
                levels[str(level.get('inventory_item_id'))] = int(
                    level['available'])
        return levels, failures

    def _rest_set_levels(self, session, set_url, headers, location_id,
                         quantities, variant_by_item):
        """One POST per item to inventory_levels/set.json.

        Returns ``(pushed, failures, retries)``.
        """
        pushed = 0
        retried = 0
        failures = []
        for inventory_item_id, available in (quantities or {}).items():
            variant_id, group_key = variant_by_item.get(
                inventory_item_id, (inventory_item_id, None))
            payload = json.dumps({
                'location_id':       location_id,
                'inventory_item_id': inventory_item_id,
                'available':         available,
            })
            try:
                resp = session.post(set_url, headers=headers, data=payload,
                                    timeout=SHOPIFY_TIMEOUT)
            except requests.exceptions.RequestException as error:
                failures.append('variant %s (group %s, location %s): %s'
                                % (variant_id, group_key, location_id,
                                   error))
                continue
            # count the retries urllib3 already absorbed, so a flaky link
            # shows up in the log even when it recovers
            raw_retries = getattr(getattr(resp, 'raw', None), 'retries', None)
            retried += len(getattr(raw_retries, 'history', ()) or ())
            if resp.status_code not in (200, 201):
                failures.append(
                    'variant %s (group %s, location %s): HTTP %s %s'
                    % (variant_id, group_key, location_id,
                       resp.status_code, (resp.text or '').strip()[:300]))
            else:
                pushed += 1
        return pushed, failures, retried

    def _graphql_set_levels(self, session, graphql_url, headers,
                            location_id, quantities):
        """Set `available` for many items at once (API 2024-07+).

        Returns ``(pushed, leftover, failures)``: `leftover` holds the
        quantities of any chunk the mutation could not place, so the
        caller can fall back to the REST endpoint for those instead of
        losing them.
        """
        pushed = 0
        leftover = {}
        failures = []
        items = list((quantities or {}).items())
        for start in range(0, len(items), GRAPHQL_SET_CHUNK):
            chunk = items[start:start + GRAPHQL_SET_CHUNK]
            variables = {'input': {
                'name': 'available',
                'reason': 'correction',
                'ignoreCompareQuantity': True,
                'quantities': [{
                    'inventoryItemId':
                        'gid://shopify/InventoryItem/%s' % item,
                    'locationId': 'gid://shopify/Location/%s' % location_id,
                    'quantity': int(available),
                } for item, available in chunk],
            }}
            error = None
            try:
                resp = session.post(
                    graphql_url, headers=headers,
                    data=json.dumps({'query': GRAPHQL_SET_QUANTITIES,
                                     'variables': variables}),
                    timeout=SHOPIFY_TIMEOUT)
            except requests.exceptions.RequestException as err:
                error = str(err)
            else:
                if resp.status_code not in (200, 201):
                    error = 'HTTP %s %s' % (resp.status_code,
                                            (resp.text or '').strip()[:300])
                else:
                    try:
                        body = resp.json() or {}
                    except ValueError:
                        body = {}
                        error = 'unreadable response'
                    if not error:
                        user_errors = (
                            (body.get('data') or {}).get(
                                'inventorySetQuantities') or {}
                        ).get('userErrors') or []
                        if body.get('errors') or user_errors:
                            error = json.dumps(
                                body.get('errors') or user_errors)[:300]
            if error:
                failures.append(
                    'bulk set of %d item(s) at location %s: %s - retried '
                    'one by one' % (len(chunk), location_id, error))
                leftover.update(dict(chunk))
            else:
                pushed += len(chunk)
        return pushed, leftover, failures

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

    def action_queue_full_inventory_push(self):
        """Queue a push of EVERY synced variant, processed in background.

        This is the 'Update All Stock' action. It takes the same route as
        the scheduled full resync - the groups are built once, the Shopify
        inventory item ids are resolved once, and the work is split into
        job.cron batches - so re-publishing a whole catalogue does not run
        inside the user's web request, where it would time out long before
        the last variant.

        The payload carries force=True, so every quantity is written even
        when Shopify already holds the same number.
        """
        self.ensure_one()
        instance = self.shopify_instance_id
        locations = self.env['shopify.location'].sudo().search([
            ('instance_id', '=', instance.id),
            ('warehouse_id', 'in', self.warehouse_ids.ids),
            ('active', '=', True),
        ]).filtered(lambda loc: loc.shopify_location_id)
        if not locations:
            raise ValidationError(_(
                'No Shopify location is mapped to the selected '
                'warehouse(s). Please map the selected warehouse(s) to a '
                'Shopify location first (use the Sync Locations wizard).'))

        groups = self._build_inventory_groups(instance)
        if groups:
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
            raise ValidationError(_(
                'No synced product found for this Shopify instance, so '
                'there is no stock to update. Import or export the '
                'products first.'))

        model = self.env['ir.model'].search(
            [('model', '=', 'sync.inventory')])
        warehouse_ids = self.warehouse_ids.ids
        batches = 0
        size = self._inventory_batch_size(len(groups))
        for start in range(0, len(groups), size):
            self.env['job.cron'].sudo().create([{
                'model_id': model.id,
                'function': 'export_inventory_to_shopify',
                'data': {
                    'groups': groups[start:start + size],
                    'warehouse_ids': warehouse_ids,
                    'force': True,
                },
                'instance_id': instance.id,
            }])
            batches += 1

        variants = sum(len(group['items']) for group in groups)
        _logger.info(
            'Update All Stock: queued %d group(s) covering %d variant(s) '
            'in %d batch(es) for instance %s',
            len(groups), variants, batches, instance.name)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Stock update queued'),
                'message': _(
                    '%(variants)s variant(s) in %(batches)s batch(es) are '
                    'being pushed to Shopify in the background. Progress '
                    'is logged under Shopify > Logs.'
                ) % {'variants': variants, 'batches': batches},
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }

    def sync_inventory(self):
        """Dispatch to the correct sync direction."""
        if not self.warehouse_ids:
            raise ValidationError(_(
                'Please select at least one warehouse before syncing.'))

        try:
            if self.import_inventory == 'shopify':
                if self.update_all_stock:
                    return self.action_queue_full_inventory_push()
                self._sync_to_shopify()
            else:
                self._sync_from_shopify()
        except requests.exceptions.RequestException as error:
            # show the user a message instead of a raw traceback dialog
            raise ValidationError(_(
                'Could not reach Shopify: %s') % error) from error

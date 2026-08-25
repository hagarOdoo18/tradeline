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
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


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
    # helpers
    # ------------------------------------------------------------------

    def _get_odoo_qty(self, product, company_id,location):
        """Sum on-hand quantity across all selected warehouses."""
        total = 0.0
        related_products = self.env['product.product'].search([('shopify_variant_sku', '=', product.barcode)]).ids
        quants = self.env['stock.quant'].sudo().search(['|',
            ('product_id',  '=', product.id),('product_id', 'in', related_products),
            ('company_id',  '=', company_id),   ('location_id', '=', location.id),
        ])

        total += sum(quants.mapped('quantity'))
        return total

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

    def _fetch_all_shopify_products(self, store_name, version, headers):
        """Fetch all products from Shopify (handles pagination)."""
        url = "https://%s/admin/api/%s/products.json" % (store_name, version)
        payload = []
        response = requests.request("GET", url, headers=headers, data=payload)
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
                response = requests.request('GET', next_url,
                                            headers=headers, data=payload)
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
        active connected instances. Instead of pushing everything in a single
        run (which can time out on large catalogues), the synced
        product/variant records are split into batches of 50 and queued as
        job.cron records with the function 'export_inventory_to_shopify'.
        _do_job then processes one batch per cron tick."""
        model = self.env['ir.model'].search(
            [('model', '=', 'sync.inventory')])
        instances = self.env['shopify.configuration'].search(
                    [('company_id', '=', self.env.company.id)])
        size = 20
        for instance in instances:
            try:
                warehouse_ids = self.env['shopify.location'].sudo().search([
                    ('instance_id', '=', instance.id),
                    ('warehouse_id', '!=', False),
                    ('active', '=', True),
                ]).mapped('warehouse_id').ids
                if not warehouse_ids:
                    continue
                sync_ids = self.env['shopify.sync'].sudo().search([
                    ('instance_id', '=', instance.id),
                    ('shopify_variant_id', '!=', False),
                    ('product_prod_id', '!=', False),
                ]).ids
                if not sync_ids:
                    continue
                # Split synced variants into batches of 50, one job per batch.
                for i in range(0, len(sync_ids), size):
                    self.env['job.cron'].sudo().create([{
                        'model_id': model.id,
                        'function': 'export_inventory_to_shopify',
                        'data': {
                            'sync_ids': sync_ids[i:i + size],
                            'warehouse_ids': warehouse_ids,
                        },
                        'instance_id': instance.id,
                    }])
                _logger.info(
                    'Shopify inventory sync: queued %d variants in %d batch(es)'
                    ' for instance %s',
                    len(sync_ids),
                    (len(sync_ids) + size - 1) // size,
                    instance.name)
            except Exception as error:
                _logger.error(
                    'Failed to queue inventory push to Shopify for instance '
                    '%s: %s', instance.name, str(error))

    @api.model
    def export_inventory_to_shopify(self, data, instance):
        """Process a single queued inventory batch (called by job.cron._do_job).

        `data` is the Json payload stored on the job.cron record and contains
        the `sync_ids` for this batch of 50 variants and the `warehouse_ids` to
        read on-hand quantities from. A transient sync.inventory record is
        created and _sync_to_shopify is called for just this batch."""
        warehouse_ids = data.get('warehouse_ids', [])
        sync_ids = data.get('sync_ids', [])
        wizard = self.sudo().create({
            'import_inventory': 'shopify',
            'shopify_instance_id': instance.id,
            'warehouse_ids': [(6, 0, warehouse_ids)],
        })
        sync_records = self.env['shopify.sync'].sudo().browse(sync_ids).exists()
        wizard._sync_to_shopify(sync_records=sync_records)

    def _sync_to_shopify(self, sync_records=None):
        """Push Odoo on-hand quantities to Shopify inventory levels.

        When `sync_records` is provided (batch mode via job.cron) only those
        variants are pushed; otherwise every synced variant for the instance is
        pushed (manual wizard mode)."""
        shopify_instance = self.shopify_instance_id
        store_name = shopify_instance.shop_name
        version    = shopify_instance.version
        headers    = shopify_instance._get_shopify_headers()
        company_id = shopify_instance.company_id.id

        # 1. Get Shopify locations mapped to the selected warehouses
        shopify_locations = self.env['shopify.location'].sudo().search([
            ('instance_id', '=', shopify_instance.id),
            ('warehouse_id', 'in', self.warehouse_ids.ids),
            ('active', '=', True),
        ])
        location_ids = [
            int(loc.shopify_location_id)

            for loc in shopify_locations
            if loc.shopify_location_id
        ]


        if not location_ids:
            raise ValidationError(_(
                'No Shopify location is mapped to the selected warehouse(s). '
                'Please map the selected warehouse(s) to a Shopify location '
                'first (use the Sync Locations wizard).'))

        # 2. Build variant_id → inventory_item_id map from Shopify products
        shopify_products = self._fetch_all_shopify_products(
            store_name, version, headers)
        variant_to_inv_item = {}
        for product in shopify_products:
            for variant in product.get('variants', []):
                variant_to_inv_item[str(variant['id'])] = (
                    variant['inventory_item_id'])

        # 3. Get the synced variants to push (batch subset or all)
        if sync_records is None:
            sync_records = self.env['shopify.sync'].sudo().search([
                ('instance_id','=', shopify_instance.id),
                ('shopify_variant_id', '!=', False),
                ('product_prod_id',   '!=', False),('product_prod_id.shopify_variant_sku','=','')
            ])

        set_url = ("https://%s/admin/api/%s/inventory_levels/set.json"
                   % (store_name, version))

        for sync in sync_records:
            if not sync.product_prod_id.shopify_variant_sku:
                inventory_item_id = variant_to_inv_item.get(
                    str(sync.shopify_variant_id))
                if not inventory_item_id:
                    continue

                # 4. Compute total on-hand qty across selected warehouses

                # 5. Set inventory level for every active Shopify location
                for location in shopify_locations:

                    total_qty = int(self._get_odoo_qty(
                        sync.product_prod_id, company_id, location.warehouse_id.lot_stock_id))

                    payload = json.dumps({
                        'location_id':        location.shopify_location_id,
                        'inventory_item_id':  inventory_item_id,
                        'available':          total_qty if total_qty > 3 else 0,
                    })
                    resp = requests.post(set_url, headers=headers, data=payload)
                    if resp.status_code not in (200, 201):
                        self.env['log.message'].sudo().create([{
                            'name': (
                                'Inventory push failed for variant %s '
                                '(location %s): %s'
                                % (sync.shopify_variant_id,
                                   location.shopify_location_id, resp.text)
                            ),
                            'shopify_instance_id': shopify_instance.id,
                            'model': 'Stock Quantity',
                        }])
                    else:
                        self.env['log.message'].sudo().create([{
                            'name': (
                                    'Inventory push done for variant %s,product %s, '
                                    '(location %s): %s qty %s'
                                    % (sync.shopify_variant_id,sync.product_prod_id.id,
                                       location.shopify_location_id, resp.text,total_qty)
                            ),
                            'shopify_instance_id': shopify_instance.id,
                            'model': 'Stock Quantity',
                        }])
                        self._cr.commit()



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

        if self.import_inventory == 'shopify':
            self._sync_to_shopify()
        else:
                       self._sync_from_shopify()

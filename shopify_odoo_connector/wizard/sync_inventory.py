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
import re
import requests
from odoo import fields, models, _
from odoo.exceptions import ValidationError


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

    def _get_odoo_qty(self, product, company_id):
        """Sum on-hand quantity across all selected warehouses."""
        total = 0.0
        for wh in self.warehouse_ids:
            quants = self.env['stock.quant'].sudo().search([
                ('location_id', '=', wh.lot_stock_id.id),
                ('product_id',  '=', product.id),
                ('lot_id',      '=', False),
                ('company_id',  '=', company_id),
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

    def _sync_to_shopify(self):
        """Push Odoo on-hand quantities to Shopify inventory levels."""
        shopify_instance = self.shopify_instance_id
        store_name = shopify_instance.shop_name
        version    = shopify_instance.version
        headers    = shopify_instance._get_shopify_headers()
        company_id = shopify_instance.company_id.id

        # 1. Fetch active Shopify locations
        loc_url = "https://%s/admin/api/%s/locations.json" % (
            store_name, version)
        loc_resp   = requests.get(loc_url, headers=headers)
        locations  = loc_resp.json().get('locations', [])
        location_ids = [loc['id'] for loc in locations if loc.get('active')]

        if not location_ids:
            raise ValidationError(_(
                'No active locations found in Shopify. '
                'Please configure at least one location.'))

        # 2. Build variant_id → inventory_item_id map from Shopify products
        shopify_products = self._fetch_all_shopify_products(
            store_name, version, headers)
        variant_to_inv_item = {}
        for product in shopify_products:
            for variant in product.get('variants', []):
                variant_to_inv_item[str(variant['id'])] = (
                    variant['inventory_item_id'])

        # 3. Get all synced variants for this instance
        sync_records = self.env['shopify.sync'].sudo().search([
            ('instance_id',       '=', shopify_instance.id),
            ('shopify_variant_id', '!=', False),
            ('product_prod_id',   '!=', False),
        ])

        set_url = ("https://%s/admin/api/%s/inventory_levels/set.json"
                   % (store_name, version))

        for sync in sync_records:
            inventory_item_id = variant_to_inv_item.get(
                str(sync.shopify_variant_id))
            if not inventory_item_id:
                continue

            # 4. Compute total on-hand qty across selected warehouses
            total_qty = int(self._get_odoo_qty(
                sync.product_prod_id, company_id))

            # 5. Set inventory level for every active Shopify location
            for location_id in location_ids:
                payload = json.dumps({
                    'location_id':        location_id,
                    'inventory_item_id':  inventory_item_id,
                    'available':          total_qty,
                })
                resp = requests.post(set_url, headers=headers, data=payload)
                if resp.status_code not in (200, 201):
                    self.env['log.message'].sudo().create([{
                        'name': (
                            'Inventory push failed for variant %s '
                            '(location %s): %s'
                            % (sync.shopify_variant_id,
                               location_id, resp.text)
                        ),
                        'shopify_instance_id': shopify_instance.id,
                        'model': 'Stock Quantity',
                    }])

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

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
import requests
from odoo import fields, models


class ProductProduct(models.Model):
    """Class for inherited model product.product.

        Methods:
            sync_shopify_product(self):
                Method to export odoo product to shopify.
    """
    _inherit = 'product.product'

    shopify_variant = fields.Char(readonly=True,
                                  string='Shopify Variant',
                                  help='Shopify id of product variant')
    shopify_instance_id = fields.Many2one('shopify.configuration',
                                          string='Shopify Instance',
                                          help='Shopify Instance id of product '
                                               'variant')
    shopify_sync_ids = fields.One2many('shopify.sync',
                                       'product_prod_id',
                                       string='Shopify Sync',
                                       help='Shopify sync ids')

    def sync_shopify_product(self):
        """Method to export odoo product to shopify."""
        instance = self.shopify_instance_id
        store_name = instance.shop_name
        version = instance.version
        product_url = 'https://%s/admin/api/%s/products.json' % (
            store_name, version)
        instance_ids = self.shopify_sync_ids.mapped('instance_id.id')
        if instance.id not in instance_ids:
            variants = []
            for line in self.attribute_line_ids.value_ids:
                line_vals = {
                    'option1': line.name,
                    'price': self.list_price,
                    'sku': self.default_code if self.default_code else None,
                    'inventory_quantity': self.qty_available,
                    'barcode': self.barcode if self.barcode else None,
                    'id': self.id,
                    'product_id': self.id,
                }
                variants.append(line_vals)
            if not variants:
                line_vals = {
                    'id': self.id,
                    'product_id': self.id,
                    'title': self.name,
                    'body_html': self.description_sale
                    if self.description_sale else '',
                    'price': self.list_price,
                    'sku': self.default_code if self.default_code else None,
                    'inventory_quantity': int(self.qty_available),
                    'unitCost': self.standard_price,
                    'product_type': 'Storable Product'
                    if self.type == 'product' else 'Consumable'
                    if self.type == 'consu' else 'Service',
                    'barcode': self.barcode if self.barcode else None,
                }
                variants.append(line_vals)
            payload = json.dumps({
                'product': {
                    'id': self.id,
                    'title': self.name,
                    'body_html': self.description_sale
                    if self.description_sale else '',
                    'sku': self.default_code if self.default_code else None,
                    'barcode': self.barcode if self.barcode else None,
                    'inventory_quantity': int(self.qty_available),
                    'product_type': 'Storable Product'
                    if self.type == 'product' else 'Consumable'
                    if self.type == 'consu' else 'Service',
                    'unitCost': self.standard_price,
                    'variants': variants,
                }
            })
            response = requests.request('POST', product_url,
                                        headers=instance._get_shopify_headers(),
                                        data=payload)
            response_rec = response.json()
            if response_rec.get('product'):
                response_product_id = response_rec['product']['id']
                self.shopify_sync_ids.sudo().create({
                    'instance_id': instance.id,
                    'shopify_product': response_product_id,
                    'product_prod_id': self.id,
                })

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


class ProductTemplate(models.Model):
    """Class for inherited model product.template.
        Methods:
            def sync_shopify_product(self):
                Method to export odoo product to shopify.
    """
    _inherit = 'product.template'

    shopify_instance_id = fields.Many2one('shopify.configuration',
                                          string="Shopify Instance",
                                          help='Shopify instance id of '
                                               'synced product.')
    synced_product = fields.Boolean(string='Synced Product',
                                    help='Will be true for the synced product.')
    shopify_product = fields.Char('Shopify Product Id', readonly=True,
                                  help='Shopify id of the product.')
    collection_ids = fields.Many2many('shopify.collection',
                                      string='Collections',
                                      readonly=True,
                                      help='Collection id of product')
    shopify_sync_ids = fields.One2many('shopify.sync',
                                       'product_id',
                                       help='Shopify sync id of product.')
    gift_card = fields.Boolean(string='Gift Card', readonly=True,
                               help='will be true for the gift card product.')

    def sync_shopify_product(self):
        """Method to export odoo product to shopify."""
        instance = self.shopify_instance_id
        store_name = instance.shop_name
        version = instance.version
        product_url = 'https://%s/admin/api/%s/products.json' % (
            store_name, version)
        instance_ids = self.shopify_sync_ids.mapped('instance_id.id')
        if instance.id not in instance_ids:
            for line in self.product_variant_ids:
                variants = []
                options = [
                    {
                        'name': item.attribute_id.name,
                        'values': item.value_ids.mapped('name')
                    }
                    for item in self.attribute_line_ids
                ]
                options_dict = {f'option{i}': value for i, value in
                                enumerate(
                                    line.product_template_variant_value_ids.
                                    mapped('name'), start=1)}
                variant_data = {
                    'title': ' / '.join(
                        line.product_template_variant_value_ids.mapped(
                            'name')) if line.product_template_variant_value_ids
                    else self.name,
                    'price': self.list_price + sum(
                        line.product_template_variant_value_ids.mapped(
                            'price_extra')) if line.
                    product_template_variant_value_ids else self.list_price,
                    'sku': self.default_code if self.default_code else None,
                    'barcode': self.barcode if self.barcode else None,
                    'inventory_quantity': int(self.qty_available),
                    'id': line.id if line else self.id,
                    'product_id': self.id,
                }
                variant_data.update(options_dict)
                variants.append(variant_data)
            payload = json.dumps({
                'product': {
                    'id': self.id,
                    'title': self.name,
                    'body_html': self.description_sale
                    if self.description_sale else '',
                    'sku': self.default_code if self.default_code else None,
                    'inventory_quantity': int(self.qty_available),
                    'product_type': 'Storable Product'
                    if self.type == 'product' else 'Consumable'
                    if self.type == 'consu' else 'Service',
                    'unitCost': self.standard_price,
                    'barcode': self.barcode if self.barcode else None,
                    'variants': variants,
                    'options': options
                }
            })
            response = requests.request('POST', product_url,
                                        headers=instance._get_shopify_headers(),
                                        data=payload)
            create_list = []
            if 'errors' not in response.json():
                for item in response.json()['product']['variants']:
                    product_prod_id = self.product_variant_ids.filtered(
                        lambda x: ', '.join(
                            x.product_template_variant_value_ids.mapped(
                                'name')) == item['title'])
                    if product_prod_id:
                        create_list.append({
                            'instance_id': instance.id,
                            'shopify_product': response.json()['product']['id'],
                            'shopify_variant_id': item['id'],
                            'product_id': self.id,
                            'product_prod_id': product_prod_id.id
                        })
                    else:
                        create_list.append({
                            'instance_id': instance.id,
                            'shopify_product': response.json()['product']['id'],
                            'shopify_variant_id': item['id'],
                            'product_id': self.id,
                        })
                self.shopify_sync_ids.sudo().create(
                    vals for vals in create_list)

    def write(self, vals):
        super().write(vals)
        if self.shopify_sync_ids:
            for config in self.env['shopify.configuration'].search(
                    [('company_id', '=', self.env.company.id)]):
                if self.shopify_sync_ids[0].shopify_product:
                    product_url = ('https://%s/admin/api/%s/products/'
                                   '%s.json') % (
                        config.shop_name, config.version,
                        self.shopify_sync_ids[0].shopify_product)
                    headers = config._get_shopify_headers()
                    for product in self:
                        variants = []
                        for line in self.env['shopify.sync'].search(
                                [('product_id', '=', product.id)]):
                            variant = {'id': line.shopify_variant_id}
                            if 'type' in vals.keys():
                                variant['sku'] = (
                                    'Storable Product'
                                    if vals['type'] == 'product' else
                                    'Consumable'
                                    if vals['type'] == 'consu' else 'Service')
                            if 'qty_available' in vals.keys():
                                variant['inventory_quantity'] = int(
                                    vals['qty_available'])
                            if 'default_code' in vals.keys():
                                variant['sku'] = vals['default_code']
                            if 'barcode' in vals.keys():
                                variant['barcode'] = vals['barcode']
                            if 'list_price' in vals:
                                variant['price'] = vals['list_price'] + sum(
                                    line.product_prod_id.
                                    product_template_variant_value_ids.mapped(
                                        'price_extra')
                                ) if line.product_prod_id else vals[
                                    'list_price']
                            variants.append(variant)
                            line_vals = {}
                            if 'type' in vals.keys():
                                line_vals['product_type'] = (
                                    'Storable Product'
                                    if vals['type'] == 'product' else
                                    'Consumable'
                                    if vals['type'] == 'consu' else 'Service')
                            if 'qty_available' in vals.keys():
                                line_vals['inventory_quantity'] = int(
                                    vals['qty_available'])
                            if 'default_code' in vals.keys():
                                line_vals['sku'] = vals['default_code']
                            if 'barcode' in vals.keys():
                                line_vals['barcode'] = vals['barcode']
                            if 'name' in vals.keys():
                                line_vals['title'] = vals['name']
                            if 'description_sale' in vals.keys():
                                line_vals['body_html'] = vals[
                                    'description_sale']
                            line_vals['variants'] = variants
                            if any(key != 'id' for key in line_vals):
                                payload = json.dumps({'product': line_vals})
                                requests.request('PUT', product_url,
                                                 headers=headers, data=payload)

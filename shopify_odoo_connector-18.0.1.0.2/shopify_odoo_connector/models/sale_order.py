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


class SaleOrder(models.Model):
    """Class for inherited model sale. order

        Methods:
            def sync_shopify_order(self):
                Method to sync odoo orders into shopify.
            action_confirm(self):
                Supering the action_confirm function inorder to confirm the
                created sale order.
    """
    _inherit = 'sale.order'

    shopify_instance_id = fields.Many2one('shopify.configuration',
                                          string="Shopify Instance",
                                          help='Shopify instance id of '
                                               'sale order.')
    shopify_sync_ids = fields.One2many('shopify.sync',
                                       'order_id',
                                       string='Shopify sync',
                                       help='Shopify sync ida of sale order.')
    shopify_order_ref = fields.Char(string='Shopify Order Id',
                                    help='Shopify id of order')

    def sync_shopify_order(self):
        """Method to sync odoo orders into shopify"""
        instance = self.shopify_instance_id
        store_name = instance.shop_name
        version = instance.version
        order_url = "https://%s/admin/api/%s/draft_orders.json" % (
            store_name, version)
        instance_ids = self.shopify_sync_ids.mapped('instance_id.id')
        if instance.id not in instance_ids:
            line_items = []
            for line in self.order_line:
                line_vals = {
                    "title": line.product_id.name,
                    "price": line.price_unit,
                    "quantity": int(line.product_uom_qty),
                }
                line_items.append(line_vals)
            payload = json.dumps({
                "draft_order": {
                    "line_items": line_items,
                    "email": self.partner_id.email,
                    "use_customer_default_address": True
                }
            })
            response = requests.request("POST", order_url,
                                        headers=instance._get_shopify_headers(),
                                        data=payload)
            response_rec = response.json()
            if response_rec.get('draft_order'):
                response_order_id = response_rec['draft_order']['id']
                response_status = response_rec['draft_order']['status']
                response_name = response_rec['draft_order']['name']
                self.shopify_sync_ids.sudo().create({
                    'instance_id': instance.id,
                    'shopify_order_ref': response_order_id,
                    'shopify_order_name': response_name,
                    'shopify_order_number': response_order_id,
                    'order_status': response_status,
                    'order_id': self.id,
                    'synced_order': True,
                })
                self.shopify_order_ref = response_order_id

    def action_confirm(self):
        """Supering the action_confirm function inorder to confirm the created
           sale order.
           boolean: returns true or false
        """
        res = super(SaleOrder, self).action_confirm()
        if self.shopify_order_ref and self.shopify_instance_id:
            instance = self.shopify_instance_id
            store_name = instance.shop_name
            version = instance.version
            order_complete_url = ("https://%s/admin/api/%s/draft_orders/"
                                  "%s/complete.json") % (
                store_name, version, self.shopify_order_ref)
            line_items = []
            for line in self.order_line:
                line_vals = {
                    "title": line.product_id.name,
                    "price": line.price_unit,
                    "quantity": int(line.product_uom_qty),
                }
                line_items.append(line_vals)
            payload = json.dumps({
                "draft_order": {"line_items": line_items,
                                "email": self.partner_id.email,
                                "id": self.shopify_order_ref,
                                "status": "completed",
                                "use_customer_default_address": True}})
            requests.request("PUT", order_complete_url,
                             headers=instance._get_shopify_headers(),
                             data=payload)
        return res

    def write(self, vals):
        super().write(vals)
        if self._context.get('skip_shopify_write'):
            return True
        for config in self.env['shopify.configuration'].search(
                [('company_id', '=', self.env.company.id)]):
            if self.shopify_sync_ids.shopify_order_ref:
                order_url = ("https://%s/admin/api/%s/draft_orders/%s.json") % (
                    config.shop_name, config.version,
                    self.shopify_sync_ids.shopify_order_ref)
                line_items = [{
                    'id': self.shopify_sync_ids.shopify_order_ref,
                    "title": line.product_id.name,
                    "price": line.price_unit,
                    "quantity": int(line.product_uom_qty),
                } for line in self.order_line]
                payload = json.dumps({
                    "draft_order": {
                        'id': self.shopify_sync_ids.shopify_order_ref,
                        "line_items": line_items,
                        "email": self.partner_id.email
                    }
                })
                requests.request("PUT", order_url,
                                 headers=config._get_shopify_headers(),
                                 data=payload)

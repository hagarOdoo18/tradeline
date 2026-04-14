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
import ast
import json
import logging
import requests
from datetime import datetime, timedelta
from babel.dates import format_date
from odoo import fields, models, _
from odoo.exceptions import ValidationError
from odoo.tools.misc import get_lang

_logger = logging.getLogger(__name__)


class ShopifyConfiguration(models.Model):
    """Class for shopify.configuration.

        Methods:
            _compute_kanban_dashboard(self):
                Method to compute data for shopify kanban dashboard.
            _compute_kanban_dashboard_graph(self):
                Method to compute shopify kanban dashboard graph.
            _compute_customer_count(self):
                Method to compute count of data that synced with shopify.
            shopify_customers(self):
                Method to view customers from shopify.
            shopify_products(self):
                Method to view products from shopify
            shopify_orders(self):
                Method to view orders from shopify
            shopify_log_message(self):
                Method to show log messages
            shopify_collection(self):
                Methods to show shopify collection
            shopify_gift_card(self):
                Methode to show shopify gift cards
            sync_shopify(self):
                Method to connect shopify instance.
            sync_shopify_all(self):
                Method of sync all button. Syncs all data from odoo to shopify.
            open_shopify_instance(self):
                Method to open shopify instance by click.
            get_shopify_configuration_details(self):
                Method to get shopify configuration details.
            get_graph(self):
                Method to compute graph values.
            _fetch_new_access_token(self):
                Method to request a fresh token from Shopify using the
                client credentials flow and persist it to the database.
            _get_valid_access_token(self):
                Method to return a valid token, fetching a new one if the
                current token is missing or expired.
            _get_shopify_headers(self):
                Method to return request headers with a valid access token.
    """
    _name = 'shopify.configuration'
    _description = 'Shopify Connector'

    name = fields.Char(string='Instance Name', required=True,
                       help='Name of the instance')
    con_endpoint = fields.Char(string='Client ID', required=True,
                               help='Shopify App Client ID from Dev Dashboard')
    consumer_key = fields.Char(string='Password',
                               help='Legacy field, no longer used for '
                                    'authentication')
    consumer_secret = fields.Char(string='Client Secret', required=True,
                                  help='Shopify App Client Secret from '
                                       'Dev Dashboard')
    shop_name = fields.Char(string='Store Name', required=True,
                            help='Name of the shop')
    version = fields.Char(string='Version', required=True,
                          help='Version of the shop')
    access_token = fields.Char(string='Access Token',
                               help='Current Shopify API access token. '
                                    'Auto-refreshed every 24 hours.')
    token_expiry = fields.Datetime(string='Token Expiry',
                                   help='Expiry date and time of the '
                                        'current access token.')
    last_synced = fields.Datetime(string='Last Synced',
                                  help='Last instance synced date')
    product_last_synced = fields.Datetime(string='Product Last Synced',
                                          help='Last product synced date')
    customer_last_synced = fields.Datetime(string='Customer Last Synced',
                                           help='Last customer synced date')
    order_last_synced = fields.Datetime(string='Order Last Synced',
                                        help='Last order synced date')
    state = fields.Selection([('new', 'Not Connected'),
                              ('sync', 'Connected'), ],
                             string='Status', readonly=True, index=True,
                             default='new', help='State of shopify instance')
    export_product = fields.Boolean(string='Export Products',
                                    help='Enable to Export Products.')
    export_customer = fields.Boolean(string='Export Customers',
                                     help='Enable to Export Customers')
    export_order = fields.Boolean(string='Export Orders',
                                  help='Enable to Export Orders')
    webhook_product = fields.Char(string='Product Creation Url',
                                  compute='_compute_webhook_product',
                                  help='Url for create product webhook')
    webhook_customer = fields.Char(string='Customer Creation Url',
                                   compute='_compute_webhook_product',
                                   help='Url for create customer webhook')
    webhook_payment = fields.Char(string='Payment Url',
                                  compute='_compute_webhook_product',
                                  help='Url for create order webhook')
    webhook_fulfillment = fields.Char(string='Fulfillment Url',
                                      compute='_compute_webhook_product',
                                      help='Url for create order fulfilment'
                                           ' webhook')
    webhook_product_update = fields.Char(string='Product Update Url',
                                         compute='_compute_webhook_product',
                                         help='Url for update product webhook')
    webhook_product_delete = fields.Char(string='Product Delete Url',
                                         compute='_compute_webhook_product',
                                         help='Url for delete product webhook')
    webhook_customer_update = fields.Char(string='Customer Update Url',
                                          compute='_compute_webhook_product',
                                          help='Url for update customer '
                                               'webhook')
    webhook_customer_delete = fields.Char(string='Customer Delete Url',
                                          compute='_compute_webhook_product',
                                          help='Url for delete customer '
                                               'webhook')
    webhook_order_create = fields.Char(string='Order Create Url',
                                       compute='_compute_webhook_product',
                                       help='Url for create order webhook')
    webhook_order_update = fields.Char(string='Order Update Url',
                                       compute='_compute_webhook_product',
                                       help='Url for update order webhook')
    webhook_order_Cancel = fields.Char(string='Order Cancel Url',
                                       compute='_compute_webhook_product',
                                       help='Url for cancel order webhook')
    webhook_order_delete = fields.Char(string='Order Delete Url',
                                       compute='_compute_webhook_product',
                                       help='Url for order delete webhook')
    webhook_order_fulfillment = fields.Char(string='Order Fulfillment Url',
                                            compute='_compute_webhook_product',
                                            help='Url for order fulfillment '
                                                 'webhook')
    webhook_order_payment = fields.Char(string='Order Payment Url',
                                        compute='_compute_webhook_product',
                                        help='Url for order payment webhook')
    webhook_order_refund = fields.Char(string='Order Refund Url',
                                       compute='_compute_webhook_product',
                                       help='Url for order refund webhook')
    webhook_draft_order_create = fields.Char(string='Draft Order Create Url',
                                             compute='_compute_webhook_product',
                                             help='Url for create draft order'
                                                  ' webhook')
    webhook_draft_order_update = fields.Char(string='Draft Order Update Url',
                                             compute='_compute_webhook_product',
                                             help='Url for update draft order '
                                                  'webhook')
    webhook_draft_order_delete = fields.Char(string='Draft Order Delete Url',
                                             compute='_compute_webhook_product',
                                             help='Url for delete draft order '
                                                  'webhook')
    webhook_collection = fields.Char(string='Collections Url',
                                     compute='_compute_webhook_product',
                                     help='Url for create collection webhook')
    webhook_fulfillment_creation = fields.Char(
        string='Fulfillment Creation Url',
        compute='_compute_webhook_product',
        help='Url for create product webhook')
    company_id = fields.Many2one('res.company', string='Company',
                                 required=True,
                                 default=lambda self: self.env.company,
                                 help='Company id')
    customer_ids = fields.One2many('res.partner',
                                   'shopify_instance_id',
                                   string='Customers', help='Customer ids')
    product_ids = fields.One2many('product.template',
                                  'shopify_instance_id',
                                  string='Products', store=True,
                                  help='Product ids')
    order_ids = fields.One2many('sale.order',
                                'shopify_instance_id',
                                string='Orders', store=True, help='Order ids')
    log_message_ids = fields.One2many('log.message',
                                      'shopify_instance_id',
                                      string='Logs', store=True,
                                      help='Log message ids')
    customer_count = fields.Integer(string='Customer Count',
                                    compute='_compute_customer_count',
                                    help='Customer count')
    product_count = fields.Integer(string='Product Count',
                                   compute='_compute_customer_count',
                                   help='Product count')
    order_count = fields.Integer(string='Order Count',
                                 compute='_compute_customer_count',
                                 help='Order count')
    log_message_count = fields.Integer(string='Order Log message Count',
                                       compute='_compute_customer_count',
                                       help='Log message count')
    collection_count = fields.Integer(string='Collections',
                                      compute='_compute_customer_count',
                                      help='Collection count')
    kanban_dashboard = fields.Text(compute='_compute_kanban_dashboard',
                                   help='field to compute kanban dashboard.')
    kanban_dashboard_graph = fields.Text(
        compute='_compute_kanban_dashboard_graph',
        help='field to compute kanban dashboard graph')
    show_on_dashboard = fields.Boolean('Show on Dashboard', default=True,
                                       help='Will shoe the data on dashboard '
                                            'if enabled.')
    color = fields.Integer(string='Color', default=0, help='Color number')
    warehouse_id = fields.Many2one('stock.warehouse', string='Warehouse',
                                   help="Warehouse to update the Inventory of "
                                        "Products")
    active = fields.Boolean(string='Active', default=True, help='Active or not')
    gift_card_count = fields.Integer('Gift Cards',
                                     compute='_compute_gift_card_count',
                                     help='Count of gift card.')
    is_exporting = fields.Boolean(string='Is Exporting',
                                  help='Will be True while exporting records')

    def _fetch_new_access_token(self):
        self.ensure_one()
        if not self.con_endpoint or not self.consumer_secret:
            raise ValidationError(_(
                'Client ID and Client Secret are required to generate an '
                'access token. Please configure them in the Shopify instance.'
            ))
        token_url = 'https://%s/admin/oauth/access_token' % self.shop_name
        payload = {
            'grant_type': 'client_credentials',
            'client_id': self.con_endpoint,
            'client_secret': self.consumer_secret,
        }
        headers = {'Content-Type': 'application/x-www-form-urlencoded'}
        try:
            response = requests.post(token_url, data=payload,
                                     headers=headers, timeout=30)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            raise ValidationError(_(
                'Failed to fetch Shopify access token: %s' % str(e)
            ))
        data = response.json()
        if 'access_token' not in data:
            raise ValidationError(_(
                'Shopify did not return an access token. Response: %s'
                % json.dumps(data)
            ))
        expires_in = data.get('expires_in', 86400)
        expiry_dt = (datetime.now() + timedelta(
            seconds=expires_in - 60)).replace(microsecond=0)
        self.with_context(skip_shopify_write=True).sudo().write({
            'access_token': data['access_token'],
            'token_expiry': expiry_dt,
        })
        self.env.cr.commit()
        _logger.info(
            'Shopify access token refreshed for instance: %s', self.name)
        return data['access_token']

    def _get_valid_access_token(self):
        self.ensure_one()
        if not self.access_token:
            return self._fetch_new_access_token()
        if self.token_expiry and datetime.now() >= self.token_expiry:
            return self._fetch_new_access_token()
        return self.access_token

    def _get_shopify_headers(self):
        return {
            'Content-Type': 'application/json',
            'X-Shopify-Access-Token': self._get_valid_access_token(),
        }

    def _compute_customer_count(self):
        """Method to compute count of data that synced with shopify."""
        for shopify in self:
            shopify.customer_count = self.env['res.partner'].search_count(
                [('shopify_sync_ids.instance_id', '=', shopify.id)])
            shopify.product_count = self.env['product.template'].search_count(
                [('shopify_sync_ids.instance_id', '=', shopify.id)])
            shopify.order_count = self.env['sale.order'].search_count(
                [('shopify_sync_ids.instance_id', '=', shopify.id)])
            shopify.log_message_count = len(shopify.log_message_ids)
            shopify.collection_count = self.env[
                'shopify.collection'].search_count(
                [('shopify_instance_id', '=', shopify.id)])
            shopify.gift_card_count = self.env[
                'product.template'].search_count(
                ['&', ('shopify_sync_ids.instance_id', '=', shopify.id),
                 ('gift_card', '=', True)])

    def _compute_gift_card_count(self):
        """Method to compute count of data that synced with shopify."""
        for shopify in self:
            shopify.customer_count = self.env['res.partner'].search_count(
                [('shopify_sync_ids.instance_id', '=', shopify.id)])
            shopify.product_count = self.env['product.template'].search_count(
                [('shopify_sync_ids.instance_id', '=', shopify.id)])
            shopify.order_count = self.env['sale.order'].search_count(
                [('shopify_sync_ids.instance_id', '=', shopify.id)])
            shopify.log_message_count = len(shopify.log_message_ids)
            shopify.collection_count = self.env[
                'shopify.collection'].search_count(
                [('shopify_instance_id', '=', shopify.id)])
            shopify.gift_card_count = self.env[
                'product.template'].search_count(
                ['&', ('shopify_sync_ids.instance_id', '=', shopify.id),
                 ('gift_card', '=', True)])

    def _compute_kanban_dashboard(self):
        """Method to compute data for shopify kanban dashboard."""
        for shopify_instance in self:
            shopify_instance.kanban_dashboard = json.dumps(
                shopify_instance.get_shopify_configuration_details())

    def _compute_kanban_dashboard_graph(self):
        """Method to compute shopify kanban dashboard graph."""
        for shopify_instance in self:
            shopify_instance.kanban_dashboard_graph = json.dumps(
                shopify_instance.get_graph())

    def _compute_webhook_product(self):
        https_url = self.env[
            'ir.config_parameter'].sudo().get_param(
            'web.base.url').replace("http", "https", 1)
        for rec in self:
            rec.webhook_product = https_url + '/products'
            rec.webhook_customer = https_url + '/customers'
            rec.webhook_payment = https_url + '/payments'
            rec.webhook_fulfillment = https_url + '/fulfillment_creation'
            rec.webhook_product_update = https_url + '/update_products'
            rec.webhook_product_delete = https_url + '/delete_products'
            rec.webhook_customer_update = https_url + '/update_customer'
            rec.webhook_customer_delete = https_url + '/delete_customer'
            rec.webhook_order_create = https_url + '/create_order'
            rec.webhook_order_update = https_url + '/update_order'
            rec.webhook_order_Cancel = https_url + '/cancel_order'
            rec.webhook_order_delete = https_url + '/delete_order'
            rec.webhook_order_fulfillment = https_url + '/order_fulfillment'
            rec.webhook_order_payment = https_url + '/order_payment'
            rec.webhook_order_refund = https_url + '/order_refund'
            rec.webhook_draft_order_create = https_url + '/draft_orders'
            rec.webhook_draft_order_update = https_url + '/draft_order_update'
            rec.webhook_draft_order_delete = https_url + '/delete_draft_order'
            rec.webhook_collection = https_url + '/collection_details'
            rec.webhook_fulfillment_creation = https_url + '/fulfillment_creation'

    def shopify_customers(self):
        """Method to view customers from shopify.
            dictionary:returns dictionary with details of ir action act window.
        """
        return {
            'name': 'Shopify Customers',
            'type': 'ir.actions.act_window',
            'res_model': 'res.partner',
            'view_mode': 'list,form',
            'domain': [('shopify_sync_ids.instance_id', '=', self.id)],
            'context': dict(self._context, create=False)
        }

    def shopify_products(self):
        """Method to view products from shopify.

            dictionary:returns dictionary with details of ir action act window.
        """
        return {
            'name': 'Shopify Products',
            'type': 'ir.actions.act_window',
            'res_model': 'product.template',
            'view_mode': 'list,form',
            'domain': [('shopify_sync_ids.instance_id', '=', self.id)],
            'context': dict(self._context, create=False)
        }

    def shopify_orders(self):
        """Method to view orders from shopify.

            dictionary:returns dictionary with details of ir action act window.
        """
        return {
            'name': 'Shopify Orders',
            'type': 'ir.actions.act_window',
            'res_model': 'sale.order',
            'view_mode': 'list,form',
            'domain': [('shopify_sync_ids.instance_id', '=', self.id)],
            'context': dict(self._context, create=False)
        }

    def shopify_log_message(self):
        """Method to view log messages.
           dictionary:returns dictionary of action type.
        """
        self.ensure_one()
        if len(self.log_message_ids) > 0:
            return {
                'name': 'Shopify Log Messages',
                'type': 'ir.actions.act_window',
                'res_model': 'log.message',
                'view_mode': 'list,form',
                'domain': [('id', 'in', self.log_message_ids.ids)],
                'context': dict(self._context, create=False)
            }
        else:
            return {
                'type': 'ir.actions.act_window_close'
            }

    def shopify_collection(self):
        """Method to view shopify collection.

            dictionary:returns dictionary with details of ir action act window.
        """
        return {
            'name': 'Collection',
            'type': 'ir.actions.act_window',
            'res_model': 'shopify.collection',
            'view_mode': 'list,form',
            'domain': [('shopify_instance_id', '=', self.id)],
            'context': dict(self._context, create=False)
        }

    def shopify_gift_card(self):
        """Method to view shopify gift cards.

            dictionary:returns dictionary with details of ir action act window.
        """
        return {
            'name': 'Shopify Gift Card Products',
            'type': 'ir.actions.act_window',
            'res_model': 'product.template',
            'view_mode': 'list,form',
            'domain': ['&', ('shopify_sync_ids.instance_id', '=', self.id),
                       ('gift_card', '=', True)],
            'context': dict(self._context, create=False)
        }

    def sync_shopify(self):
        """Method to connect shopify instance."""
        try:
            store_name = self.shop_name
            version = self.version
            token = self._get_valid_access_token()
            url = 'https://%s/admin/api/%s/storefront_access_tokens.json' % (
                store_name, version)
            payload = json.dumps({
                'storefront_access_token': {
                    'title': 'Test'
                }
            })
            headers = {
                'Content-Type': 'application/json',
                'X-Shopify-Access-Token': token,
            }
            response = requests.request('POST', url, headers=headers,
                                        data=payload)
            if response.status_code == 200:
                self.state = 'sync'
            else:
                raise ValidationError(_(
                    'Invalid Credentials provided. Please check them.'))
        except ValidationError:
            raise
        except Exception as e:
            raise ValidationError(_('Something went wrong: %s' % e))

    def sync_shopify_all(self):
        """Method of sync all button. Syncs all data from odoo to shopify."""
        if (not self.export_product and not self.export_customer
                and not self.export_order):
            raise ValidationError(_(
                'Select an Export option from shopify configuration'))
        else:
            store_name = self.shop_name
            version = self.version
            self.is_exporting = True
            self.env.cr.commit()
            if self.export_product:
                product_url = 'https://%s/admin/api/%s/products.json' % (
                    store_name, version)
                self.ensure_one()
                product = self.env['product.template'].sudo().search(
                    []).filtered(
                    lambda x: x.id not in self.env[
                        'shopify.sync'].sudo().search(
                        [('instance_id', '=', self.id)]).mapped(
                        'product_id').ids)
                for rec in product:
                    options = []
                    variants = []
                    for line in rec.product_variant_ids:
                        options = [
                            {
                                'name': item.attribute_id.name,
                                'values': item.value_ids.mapped('name')
                            }
                            for item in rec.attribute_line_ids
                        ]
                        options_dict = {f'option{i}': value for i, value in
                                        enumerate(
                                            line.
                                            product_template_variant_value_ids.
                                            mapped('name'), start=1)}
                        variant_data = {
                            'title': ' / '.join(
                                line.product_template_variant_value_ids.mapped(
                                    'name')) if line.
                            product_template_variant_value_ids else rec.name,
                            'price': rec.list_price + sum(
                                line.product_template_variant_value_ids.mapped(
                                    'price_extra')) if line.
                            product_template_variant_value_ids else
                            rec.list_price,
                            'sku': rec.default_code if rec.default_code
                            else None,
                            'barcode': rec.barcode if rec.barcode else None,
                            'inventory_quantity': int(rec.qty_available),
                            'id': line.id if line else rec.id,
                            'product_id': rec.id,
                        }
                        variant_data.update(options_dict)
                        variants.append(variant_data)
                    payload = json.dumps({
                        'product': {
                            'id': rec.id,
                            'title': rec.name,
                            'body_html': rec.description_sale
                            if rec.description_sale else '',
                            'sku': rec.default_code if rec.default_code
                            else None,
                            'inventory_quantity': int(rec.qty_available),
                            'product_type': 'Storable Product'
                            if rec.type == 'product' else 'Consumable'
                            if rec.type == 'consu' else 'Service',
                            'unitCost': rec.standard_price,
                            'barcode': rec.barcode if rec.barcode else None,
                            'variants': variants,
                            'options': options
                        }
                    })
                    response = requests.request('POST', product_url,
                                                headers=self._get_shopify_headers(),
                                                data=payload)
                    response_rec = response.json()
                    if 'errors' not in response_rec:
                        for item in response_rec['product']['variants']:
                            product_prod_id = rec.product_variant_ids.filtered(
                                lambda x: ', '.join(
                                    x.product_template_variant_value_ids.mapped(
                                        'name')) == item['title'] or ' / '.join(
                                    x.product_template_variant_value_ids.mapped(
                                        'name')) == item['title'])
                            if product_prod_id:
                                product_prod_id.shopify_sync_ids.sudo().create({
                                    'instance_id': self.id,
                                    'shopify_product':
                                        response_rec['product']['id'],
                                    'shopify_variant_id': item['id'],
                                    'product_id': rec.id,
                                    'product_prod_id': product_prod_id.id
                                })
                            else:
                                if not product.shopify_sync_ids.search(
                                        [('instance_id', '=', self.id),
                                         ('shopify_product', '=',
                                          response_rec['product']['id']),
                                         ('product_id', '=', rec.id)]):
                                    product.shopify_sync_ids.sudo().create({
                                        'instance_id': self.id,
                                        'shopify_variant_id': item['id'],
                                        'shopify_product':
                                            response_rec['product']['id'],
                                        'product_id': rec.id,
                                    })
            if self.export_customer:
                customer_url = ('https://%s/admin/api/%s/customers.json' % (
                    store_name, version))
                partner = self.env['res.partner'].sudo().search([]).filtered(
                    lambda x: x.id not in self.env[
                        'shopify.sync'].sudo().search(
                        [('instance_id', '=', self.id)]).mapped(
                        'customer_id').ids)
                for customer in partner:
                    payload = json.dumps({
                        'customer': {
                            'first_name': customer.name,
                            'last_name': '',
                            'email': customer.email if customer.email else '',
                            'phone': customer.phone if customer.phone else '',
                            'verified_email': True,
                            'addresses': [
                                {
                                    'address1': customer.street if
                                    customer.street else '',
                                    'city': customer.city if customer.city
                                    else '',
                                    'province': '',
                                    'phone': customer.phone if customer.phone
                                    else '',
                                    'zip': customer.zip if customer.zip else '',
                                    'last_name': '',
                                    'first_name': customer.name if
                                    customer.name else '',
                                    'country': customer.country_id.name if
                                    customer.country_id.name else ''
                                }
                            ],
                            'send_email_invite': True
                        }
                    })
                    response = requests.request('POST', customer_url,
                                                headers=self._get_shopify_headers(),
                                                data=payload)
                    response_rec = response.json()
                    if 'errors' not in response_rec.keys():
                        customer.shopify_sync_ids.sudo().create({
                            'instance_id': self.id,
                            'shopify_customer_ref': response_rec['customer'][
                                'id'],
                            'customer_id': customer.id
                        })
            if self.export_order:
                order_url = ('https://%s/admin/api/%s/draft_orders.json' % (
                    store_name, version))
                sale_order = self.env['sale.order'].sudo().search(
                    [('state', '=', 'draft')]).filtered(
                    lambda x: x.id not in self.env[
                        'shopify.sync'].sudo().search(
                        [('instance_id', '=', self.id)]).mapped(
                        'order_id').ids)
                for order in sale_order:
                    line_items = []
                    for line in order.order_line:
                        line_vals = {
                            'title': line.product_id.name,
                            'price': line.price_unit,
                            'quantity': int(line.product_uom_qty),
                        }
                        line_items.append(line_vals)
                    payload = json.dumps({
                        'draft_order': {
                            'line_items': line_items,
                            'email': order.partner_id.email,
                            'use_customer_default_address': True
                        }
                    })
                    response = requests.request('POST', order_url,
                                                headers=self._get_shopify_headers(),
                                                data=payload)
                    response_rec = response.json()
                    if 'errors' not in response_rec.keys():
                        response_order_id = response_rec['draft_order']['id']
                        response_status = response_rec['draft_order']['status']
                        response_name = response_rec['draft_order']['name']
                        order.shopify_sync_ids.sudo().create({
                            'instance_id': self.id,
                            'shopify_order_ref': response_order_id,
                            'shopify_order_name': response_name,
                            'shopify_order_number': response_order_id,
                            'order_status': response_status,
                            'order_id': order.id,
                            'synced_order': True,
                        })
                        order.shopify_order_ref = response_order_id
            self.is_exporting = False
            self.last_synced = datetime.now()

    def open_shopify_instance(self):
        """Method to open shopify instance.
          dictionary: returns dictionary of action values
        """
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'shopify_odoo_connector.action_shopify_configuration')
        context = self._context.copy()
        if 'context' in action and type(action['context']) == str:
            context.update(ast.literal_eval(action['context']))
        else:
            context.update(action.get('context', {}))
        action['context'] = context
        action['domain'] = [('id', '=', self.id)]
        return action

    def get_shopify_configuration_details(self):
        """Method to get shopify configuration details.
            dictionary: returns dictionary of configuration values.
        """
        customer_count = self.customer_count
        product_count = self.product_count
        sale_count = self.order_count
        sale_income_this_month = 0.0
        sale_income_this_year = 0.0
        sale_income_last_month = 0.0
        return {
            'customer_count': customer_count,
            'product_count': product_count,
            'sale_count': sale_count,
            'sale_income_this_year': sale_income_this_year,
            'sale_income_this_month': sale_income_this_month,
            'sale_income_last_month': sale_income_last_month,
            'company_count': 1,
        }

    def get_graph(self):
        """Method to compute graph values.
            dictionary: returns dictionary of values needed for graph.
        """

        def graph_data(date, amount):
            nm = format_date(date, 'd LLLL Y', locale=locale)
            short_nm = format_date(date, 'd MMM', locale=locale)
            return {'x': short_nm, 'y': amount, 'name': nm}

        data = []
        locale = get_lang(self.env).code
        today = datetime.today()
        for i in range(30, 0, -5):
            current_date = today + timedelta(days=-i)
            data.append(graph_data(current_date, 0))
        return [
            {'values': data, 'title': '', 'key': 'Sale Income', 'area': True,
             'color': '#7c7bad', 'is_sample_data': False}]

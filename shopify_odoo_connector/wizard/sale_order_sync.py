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
import dateutil.parser
import json
import logging
import pytz
import re
import requests
import odoo
from odoo import api, models, fields, _
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class SaleOrderSync(models.TransientModel):
    """ Class for transient model sale order sync

        Methods:
            sync_orders(self):
                method to create  job for exporting orders.It will also
                call the methods to create  jobs for importing orders.
            sync_confirmed_orders(self):
                method to create  jobs for importing confirmed orders.
            sync_draft_orders(self):
                method to create  jobs for importing draft orders.
            import_confirmed_orders_from_shopify(self,shopify_orders):
                method to import confirmed orders from shopify to odoo.
                 job evokes this method for creating confirmed orders
                in odoo.
            import_draft_orders_from_shopify(self,shopify_orders):
                method to import draft orders from shopify to odoo. job
                evokes this method for creating draft orders in odoo.
            export_orders_to_shopify(self,sale_order):
                method to export orders from odoo to shopify job
                evokes this method to export odoo orders.
    """
    _name = 'sale.order.sync'
    _description = 'Sale Order Sync'

    import_orders = fields.Selection(string='Import/Export',
                                     selection=[('shopify', 'To Shopify'),
                                                ('odoo', 'From Shopify')],
                                     help='Select the operation',
                                     required=True, default='odoo')
    shopify_instance_id = fields.Many2one('shopify.configuration',
                                          string="Shopify Instance",
                                          help='Choose the Shopify instance',
                                          required=True)
    draft = fields.Boolean(string='Draft Orders', help='True for draft orders')
    type_order = fields.Selection(string='Type of order',
                                  help='Choose the type of order',
                                  selection=[('draft', 'Draft Orders'),
                                             ('confirmed', 'Confirmed Orders')],
                                  required=True, default='draft')

    def sync_orders(self):
        """ method to create  job for exporting orders.It will also
            call the methods to create  jobs for importing orders."""
        model = self.env['ir.model'].search([('model', '=', "sale.order.sync")])
        shopify_instance = self.shopify_instance_id
        if (self.import_orders == 'shopify' and
                not self.shopify_instance_id.export_order):
            raise ValidationError(_(
                'For Syncing Orders to Shopify Enable Export Orders option '
                'in shopify configuration '))
        else:
            if self.import_orders == 'shopify':
                sale_order = self.env['sale.order'].search(
                    [('state', '=', 'draft'),
                     ('company_id', 'in',
                      [False, shopify_instance.company_id.id])])
                order_list = []
                order_list_id = []
                size = 50
                for rec in range(0, len(sale_order), size):
                    order_list.append(sale_order[rec:rec + size])
                for order in order_list:
                    for item in order:
                        order_list_id.append(item.id)
                    self.env['job.cron'].sudo().create([{
                        'model_id': model.id,
                        'function': "export_orders_to_shopify",
                        'data': order_list_id,
                        'instance_id': self.shopify_instance_id.id,
                        'wizard': self.id,
                    }])
                    order_list_id = []
            else:
                if self.type_order == 'draft':
                    self.sync_draft_orders(self.shopify_instance_id)
                elif self.type_order == 'confirmed':
                    self.sync_confirmed_orders(self.shopify_instance_id,
                                               self.id)

    @api.model
    def _cron_sync_confirmed_orders(self):
        """Scheduled action to import confirmed orders from Shopify for all
        active connected instances. Mirrors the manual 'From Shopify ->
        Confirmed Orders' wizard flow: for each instance a transient
        sale.order.sync record is created and sync_confirmed_orders is
        called, which queues import_confirmed_orders_from_shopify job.cron
        records that _do_job then processes."""
        instances = self.env['shopify.configuration'].search(
            [('company_id', '=', self.env.company.id)])
        for instance in instances:
            try:
                wizard = self.sudo().create({
                    'import_orders': 'odoo',
                    'shopify_instance_id': instance.id,
                    'type_order': 'confirmed',
                })
                wizard.sync_confirmed_orders(instance, wizard.id)
            except Exception as error:
                _logger.error(
                    'Failed to queue confirmed orders sync for Shopify '
                    'instance %s: %s', instance.name, str(error))

    def sync_confirmed_orders(self, instance, ref):
        """Method to create  jobs for importing confirmed orders."""
        model = self.env['ir.model'].search([('model', '=', "sale.order.sync")])
        store_name = instance.shop_name
        version = instance.version
        order_url = "https://%s/admin/api/%s/orders.json" % (
            store_name, version)
        payload = []
        headers = instance._get_shopify_headers()
        response = requests.request("GET", order_url,
                                    headers=headers,
                                    data=payload)
        if 'orders' in response.json():
            shopify_orders = response.json()['orders']
            self.env['job.cron'].sudo().create([{
                'model_id': model.id,
                'function': "import_confirmed_orders_from_shopify",
                'data': shopify_orders,
                'instance_id': self.shopify_instance_id.id,
                'wizard': ref,
            }])
        order_link = response.headers[
            'link'] if 'link' in response.headers else ''
        order_links = order_link.split(',')
        for link in order_links:
            match = re.compile(r'rel=\"next\"').search(link)
            if match:
                order_link = link
        rel = re.search('rel=\"(.*)\"', order_link).group(
            1) if 'link' in response.headers else ''
        if order_link and rel == 'next':
            item = 0
            rec = 1
            while item < rec:
                page_info = re.search('page_info=(.*)>', order_link).group(1)
                limit = 50
                order_link = ("https://%s/admin/api/%s/orders.json?"
                              "limit=%s&page_info=%s") % (
                                  store_name, version, limit, page_info)
                response = requests.request('GET', order_link,
                                            headers=headers, data=payload)
                if 'orders' in response.json():
                    orders = response.json()['orders']
                    self.env['job.cron'].sudo().create({
                        'model_id': model.id,
                        'function': "import_confirmed_orders_from_shopify",
                        'data': orders,
                        'instance_id': self.shopify_instance_id.id,
                        'wizard': ref,
                    })
                order_link = response.headers['link']
                order_links = order_link.split(',')
                for link in order_links:
                    match = re.compile(r'rel=\"next\"').search(link)
                    if match:
                        order_link = link
                rel = re.search('rel=\"next\"', order_link)
                item += 1
                if order_link and rel is not None:
                    rec += 1

    def sync_draft_orders(self, instance):
        """Method to create  jobs for importing draft orders."""
        model = self.env['ir.model'].search([('model', '=', "sale.order.sync")])
        store_name = instance.shop_name
        version = instance.version
        order_url = "https://%s/admin/api/%s/draft_orders.json" % (
            store_name, version)
        payload = []
        headers = instance._get_shopify_headers()
        response = requests.request("GET", order_url,
                                    headers=headers,
                                    data=payload)
        if 'draft_orders' in response.json() and response.json(
        )['draft_orders']:
            shopify_orders = response.json()['draft_orders']
            self.env['job.cron'].sudo().create([{
                'model_id': model.id,
                'function': "import_draft_orders_from_shopify",
                'data': shopify_orders,
                'instance_id': instance.id,
            }])
        order_link = response.headers[
            'link'] if 'link' in response.headers else ''
        order_links = order_link.split(',')
        for link in order_links:
            match = re.compile(r'rel=\"next\"').search(link)
            if match:
                order_link = link
        rel = re.search('rel=\"(.*)\"', order_link).group(
            1) if 'link' in response.headers else ''
        if order_link and rel == 'next':
            index = 0
            rec = 1
            while index < rec:
                page_info = re.search('page_info=(.*)>', order_link).group(1)
                limit = re.search('limit=(.*)&', order_link).group(1)
                order_link = ("https://%s/admin/api/%s/draft_orders.json?"
                              "limit=%s&page_info=%s") % (
                                  store_name, version, limit, page_info)
                response = requests.request('GET', order_link,
                                            headers=headers, data=payload)
                if 'draft_orders' in response.json():
                    orders = response.json()['draft_orders']
                    self.env['job.cron'].sudo().create([{
                        'model_id': model.id,
                        'function': "import_draft_orders_from_shopify",
                        'data': orders,
                        'instance_id': instance.id,
                    }])
                order_link = response.headers['link']
                order_links = order_link.split(',')
                for link in order_links:
                    match = re.compile(r'rel=\"next\"').search(link)
                    if match:
                        order_link = link
                rel = re.search('rel=\"next\"', order_link)
                index += 1
                if order_link and rel is not None:
                    rec += 1

    def _get_shopify_order_warehouse(self, code, instance):
        """Resolve the Odoo warehouse a Shopify order should be assigned to.

        Shopify orders carry the fulfillment location either as the
        order-level 'location_id' (often None while the order is still
        unpaid / not yet fulfilled) or, for storefronts that let the
        customer pick a branch/pickup location, as a '_selected_location'
        note attribute (e.g. 'Tradeline Sodic'). Both are matched against
        the shopify.location mapping table (warehouse <-> Shopify location)
        for this instance. Falls back to the instance's default warehouse.

            each(dict): raw Shopify order payload.
            instance(shopify.configuration): the Shopify instance.

            stock.warehouse: the resolved warehouse (may be an empty
            recordset if nothing could be matched and no default is set).
        """
        location_model = self.env['shopify.location'].sudo()

        location_id = code
        if location_id:
            numeric_id = str(location_id).rsplit('/', 1)[-1]
            loc = location_model.search([
                ('instance_id', '=', instance.id),
                '|',
                ('shopify_location_id', '=', numeric_id),
                ('name', '=ilike', str(location_id)),
            ], limit=1)
            if loc.warehouse_id:
                return loc.warehouse_id





        return instance.warehouse_id

    def _pick_product_with_stock(self, product, warehouse, company_id,
                                 qty_needed):
        """Return the variant that can actually be served from `warehouse`.

        One Shopify variant can be mirrored by several Odoo products: the
        "master" product carries the barcode, its aliases carry that same
        value in `shopify_variant_sku`. They are interchangeable for
        fulfilment, so when the product resolved from the Shopify payload has
        no free stock in the order's warehouse, switch to a sibling that does.
        Falls back to the originally resolved product when no sibling can
        cover the line either, so behaviour is unchanged for single-variant
        products.
        """
        if not product or not warehouse or not warehouse.lot_stock_id:
            return product

        barcode = product.barcode or product.shopify_variant_sku
        if not barcode:
            return product

        siblings = self.env['product.product'].sudo().search([
            '|',
            ('barcode', '=', barcode),
            ('shopify_variant_sku', '=', barcode),
            ('id', 'not in', product.ids),
            ('company_id', 'in', [company_id, False]),
        ])
        if not siblings:
            return product

        location = warehouse.lot_stock_id

        def _free_qty(candidate):
            quants = self.env['stock.quant'].sudo().search([
                ('product_id', '=', candidate.id),
                ('company_id', '=', company_id),
                ('location_id', 'child_of', location.id),
            ])
            return (sum(quants.mapped('quantity'))
                    - sum(quants.mapped('reserved_quantity')))

        # Keep the resolved product when it can cover the line itself.
        if _free_qty(product) >= qty_needed:
            return product

        # Otherwise take the sibling with the most free stock that still
        # covers the ordered quantity.
        scored = [(candidate, _free_qty(candidate)) for candidate in siblings]
        scored.sort(key=lambda item: item[1], reverse=True)
        for candidate, free in scored:
            if free >= qty_needed:
                return candidate
        return product

    def import_confirmed_orders_from_shopify(self, shopify_orders, instance,
                                             ref):
        """ Method to import confirmed orders from shopify to odoo.
             job evokes this method for creating confirmed orders in odoo.

            get_shopify_orders(list):list of dictionary with orders values.
        """
        wizard = self.env['sale.order.sync'].sudo().browse(ref)
        shopify_instance = instance
        store_name = instance.shop_name
        version = instance.version
        headers = instance._get_shopify_headers()
        for each in shopify_orders:
            # Build a fresh vals dict for every order. Odoo's sale.order
            # create() writes the generated sequence back into vals
            # (vals['name'] = 'S00xxx'); a shared dict would keep that name
            # and reuse the same sequence for every subsequent order. A fresh
            # dict also prevents fields (shipping/billing, taxes, warehouse)
            # from leaking between orders.
            vals = {}
            shopify_id = each['id']
            # One savepoint per order: a failure rolls back only this order
            # (and leaves the cursor usable), so the reason can be logged and
            # the next order still imported.
            savepoint = self.env.cr.savepoint()
            try:
                # Serialize imports of the same Shopify order across the
                # scheduled importer and the real-time endpoint. Re-check the
                # instance-specific sync only after obtaining the lock.
                normalized_shopify_id = str(shopify_id).rsplit('/', 1)[-1]
                self.env.cr.execute(
                    'SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
                    ('shopify-order:%s:%s' % (
                        instance.id, normalized_shopify_id),))
                existing_sync = self.env['shopify.sync'].sudo().search([
                    ('instance_id', '=', instance.id),
                    ('shopify_order_ref', '=', str(shopify_id)),
                    ('order_id', '!=', False),
                ], limit=1)
                existing_order = existing_sync.order_id
                if not existing_order:
                    self._normalize_shopify_order(each, instance)
                    shipping_address = each['shipping_address'] or {}
                    billing_address = each['billing_address'] or {}
                    # an order without a customer (or with an unknown one)
                    # gets a partner found or created from its own data
                    partner_id = self._find_or_create_order_partner(
                        each, shopify_instance).id
                    vals["partner_id"] = partner_id
                    if shipping_address:
                        county_id = self.env['res.country'].search([
                            ('name', '=',
                             shipping_address.get('country'))
                        ], limit=1)
                        state_id = self.env['res.country.state'].search([
                            ('name', '=',
                             shipping_address.get('province'))
                        ],limit=1)
                        shipping_child_id = self.env[
                            'res.partner'].sudo().create([
                            {"name": shipping_address.get('first_name') if shipping_address.get('first_name') else '',
                             "street": shipping_address.get('address1') if shipping_address.get('address1') else '',
                             "street2": shipping_address.get('address2') if shipping_address.get('address2') else '',
                             "city": shipping_address.get('city') if
                             shipping_address.get('city') else '',
                             "state_id": state_id.id or None,
                             "phone": shipping_address.get('phone') if
                             shipping_address.get('phone') else None,
                             "zip": shipping_address.get('zip') if
                             shipping_address.get('zip') else '',
                             "country_id": county_id.id or None,
                             "parent_id": partner_id,
                             "type": 'delivery',
                             }]).id
                        vals['partner_shipping_id'] = shipping_child_id
                    if billing_address and shipping_address :

                        county_id = self.env['res.country'].search([
                            ('name', '=',
                             shipping_address.get('country'))
                        ], limit=1)
                        state_id = self.env['res.country.state'].search([
                            ('name', '=',
                             shipping_address.get('province'))
                        ],limit=1)
                        billing_child_id = self.env[
                            'res.partner'].sudo().create(
                            [{
                                "name": billing_address.get('first_name') if
                                billing_address.get('first_name') else '',
                                "street": billing_address.get('address1') if
                                billing_address.get('address1') else '',
                                "street2": billing_address.get('address2') if billing_address.get('address2') else '',
                                "city": billing_address.get('city') if
                                billing_address.get('city') else '',
                                "state_id": state_id.id or None,
                                "phone": billing_address.get('phone') if
                                billing_address.get('phone') else None,
                                "zip": billing_address.get('zip') if
                                billing_address.get('zip') else '',
                                "country_id": county_id.id or None,
                                "parent_id": partner_id,
                                "type": 'invoice'}]).id
                        vals['partner_invoice_id'] = billing_child_id
                    if each['tax_lines']:
                        tax = each['tax_lines'][0]['rate']
                        tax_group = each['tax_lines'][0]["title"]
                        taxes = tax * 100
                        tax_name = self.env[
                            'account.tax'].search(
                            [('amount', '=', taxes),
                             ('type_tax_use', '=', 'sale'),
                             ('company_id', '=', instance.company_id.id)],
                            limit=1)
                        if not tax_name:
                            tax_group_id = self.env['account.tax.group'].create(
                                {'name': tax_group})
                            tax_name = self.env['account.tax'].create(
                                [{'name': tax_group + str(taxes) + '%',
                                  'type_tax_use': 'sale',
                                  'amount_type': 'percent',
                                  'tax_group_id': tax_group_id.id,
                                  'amount': taxes,
                                  }])
                    else:
                        tax_name = instance.default_sale_tax_id
                    vals["date_order"] = str(odoo.fields.Datetime.to_string(
                        dateutil.parser.parse(each['created_at']).astimezone(
                            pytz.utc)))
                    vals["shopify_order_ref"] = each['id']
                    vals["reference_number"] = each['name']
                    # vals["name"] = each['name']
                    vals['shopify_instance_id'] = shopify_instance.id
                    note_parts = []
                    if each.get('note'):
                        note_parts.append(each['note'])
                    for attr in each.get('note_attributes') or []:
                        note_parts.append('%s: %s' % (
                            attr.get('name') or '', attr.get('value') or ''))
                    if each.get('payment_gateway_names'):
                        note_parts.append('Payment Gateway: %s' % ', '.join(
                            each['payment_gateway_names']))
                    if note_parts:
                        vals['note'] = '\n'.join(note_parts)

                    fulfillment_status = each['fulfillment_status']
                    payment_status = each['financial_status']
                    fulfillment = 'fulfilled' \
                        if fulfillment_status == 'fulfilled' \
                        else 'partially_fulfilled' \
                        if fulfillment_status == 'partially_fulfilled' \
                        else 'un_fulfilled'
                    payment = 'paid' if payment_status == 'paid' \
                        else 'partially_paid' \
                        if payment_status == 'partially_paid' \
                        else 'partially_refunded' \
                        if payment_status == 'partially_refunded' \
                        else 'refunded' if payment_status == 'refunded' \
                        else 'unpaid'
                    shipping_lines = each['shipping_lines']

                    product_id = self.env.ref(
                        'shopify_odoo_connector.product_shopify_shipping_cost')
                    # no shipping line -> no location code: the resolver
                    # falls back to the instance's default warehouse
                    line = shipping_lines[0] if shipping_lines else {}

                    order_warehouse = self._get_shopify_order_warehouse(
                        line.get('code'), shopify_instance)
                    vals['warehouse_id'] = (
                        order_warehouse.id if order_warehouse else False)
                    team = self.env['crm.team'].search(
                        [('branch_id', '=', order_warehouse.branch_id.id),
                         ('company_id', '=', self.env.company.id)])
                    vals['team_id'] = team.id if team else False
                    sales_rep = self.env['sales.rep'].search(
                        [('company_id', '=', instance.company_id.id),
                         ('is_online', '=', True)], limit=1)
                    vals['sales_rep_id'] = sales_rep.id if sales_rep else False

                    vals['branch_id'] = (
                        order_warehouse.branch_id.id if order_warehouse else False)
                    user_id = self.env['res.users'].search([('branch_id','=',order_warehouse.branch_id.id)]).id if  order_warehouse.branch_id.id !=88 else 66
                    vals['user_id'] = user_id if user_id else False
                    vals['invoice_journal_id'] = self.env['account.journal'].search([('type', '=', 'sale'),('branch_id', '=',  order_warehouse.branch_id.id)], limit=1).id
                    sale_order = self.env['sale.order']
                    so = sale_order.create(vals)

                    so.shopify_sync_ids.sudo().create(
                        {
                            'instance_id': instance.id,
                            'shopify_order_ref': each['id'],
                            'shopify_order_name': each['name'],
                            'shopify_order_number': each['number'],
                            'fulfillment_status': fulfillment,
                            'payment_status': payment,
                            'order_id': so.id,
                            'synced_order': True,
                        })
                    so.shopify_order_ref = each['id']
                    currency = self.env['res.currency'].sudo().search(
                        [('name', 'ilike', each['currency']),
                         ('active', 'in', [False, True])], limit=1)
                    if currency and not currency.active:
                        currency.sudo().write({'active': True})
                    line_vals_list = []
                    for line in each['line_items']:
                        discount = 0.0
                        if line['discount_allocations']:
                            discount = line['discount_allocations'][0]['amount']
                        product_id = self.env['product.product'].sudo().search(
                            [('barcode', '=', line['sku']),
                             ('shopify_sync_ids.instance_id', '=',
                              shopify_instance.id),
                             ('company_id', 'in', [shopify_instance.company_id.id,
                                                   False])])
                        if line['variant_id']:
                            # narrow within the barcode+instance result first
                            variant_match = product_id.filtered(
                                lambda p, vid=line['variant_id']: (
                                    str(p.shopify_variant) == str(vid)))
                            if variant_match:
                                product_id = variant_match[:1]
                            else:
                                # fall back: any product in this company with
                                # the matching Shopify variant id
                                product_id = self.env[
                                    'product.product'].sudo().search([
                                        ('shopify_variant', '=',
                                         line['variant_id']),
                                        ('company_id', 'in', [
                                            shopify_instance.company_id.id,
                                            False]),
                                    ], limit=1)
                        else:
                            product_id = product_id[:1]
                        if not product_id:
                            product = line['product_id']
                            product_response = self.env[
                                'sync.product'].create_product_by_id(
                                shopify_instance, store_name, version, product)
                            if 'errors' in product_response:
                                self.env['log.message'].sudo().create([{
                                    'name': ' Creation of product  order : ' + each[
                                        'name'] + ' with product id:  ' + str(
                                        line['id']) + ' and name:  ' + line[
                                                'title'] + '  is not processed. '
                                                           'Product does not '
                                                           'exists in Shopify.',
                                    'shopify_instance_id': instance.id,
                                    'model': 'sale.order',
                                }])
                                continue
                            # re-fetch the newly created product
                            if line['variant_id']:
                                product_id = self.env[
                                    'product.product'].sudo().search([
                                        ('shopify_variant', '=',
                                         line['variant_id']),
                                    ], limit=1)
                            else:
                                product_id = self.env[
                                    'product.product'].sudo().search([
                                        ('barcode', '=', line['sku']),
                                    ], limit=1)
                        # final guard -- skip line if product is still not resolved
                        if not product_id:
                            self.env['log.message'].sudo().create([{
                                'name': ' Order : ' + each[
                                    'name'] + ' line "' + (
                                    line.get('title') or '') + '" skipped'
                                    ' - product not found'
                                    ' (sku: ' + str(line.get('sku') or '') + ','
                                    ' variant: ' + str(
                                    line.get('variant_id') or '') + ').',
                                'shopify_instance_id': instance.id,
                                'model': 'sale.order',
                            }])
                            continue
                        # Several Odoo products can mirror the same
                        # Shopify variant; prefer the one that actually
                        # has free stock in this order's warehouse.
                        product_id = self._pick_product_with_stock(
                            product_id, order_warehouse,
                            shopify_instance.company_id.id,
                            float(line['quantity']))
                        str_list = []
                        for desc_index in line['discount_allocations']:
                            discount_type = \
                                each['discount_applications'][
                                    desc_index['discount_application_index']][
                                    'type']
                            if discount_type == 'discount_code':
                                str_list.append(
                                    each['discount_applications'][
                                        desc_index['discount_application_index']][
                                        'code'])
                            else:
                                str_list.append(
                                    each['discount_applications'][
                                        desc_index['discount_application_index']][
                                        'title'])
                        price=float(line['price'])
                        line_vals = {
                            'product_id': product_id.id,
                            'name': line.get('title') or product_id.name or '/',
                            'price_unit':price,
                            'product_uom_qty': line['quantity'],
                            'currency_id': currency.id,
                            'discount': (
                                float(discount) * 100 /
                                ((price))

                                if price and float(line['quantity'])
                                else 0
                            ) if discount else 0,
                            'tax_id': [(6, 0, tax_name.ids)] if tax_name else False,
                            'shopify_line_ref': line['id'],
                            'shopify_instance_id': shopify_instance.id,
                            'shopify_taxable': line['taxable'],
                            'shopify_tax_amount': float(
                                line['tax_lines'][0]['price']) if
                            line['tax_lines'] else 0.0,
                            'shopify_discount_amount':
                                sum(float(i['amount']) for
                                    i in line['discount_allocations']) if line[
                                    'discount_allocations'] else 0.0,
                            'shopify_line_item_discount':
                                sum(float(
                                    each['discount_applications'][
                                        i['discount_application_index']][
                                        'value']) for i in
                                    line['discount_allocations']) if
                                (each.get('discount_applications') and
                                 line['discount_allocations']) else 0.0,
                            'shopify_discount_code': ','.join(str_list),
                            'order_id': so.id,
                            'company_id': shopify_instance.company_id.id,
                        }
                        if 'refunds' in each.keys():
                            for refunds in each['refunds']:
                                for refund_line in refunds['refund_line_items']:
                                    if refund_line['line_item_id'] == line['id']:
                                        line_vals['product_uom_qty'] -= \
                                            refund_line['quantity']
                        line_vals_list.append(line_vals)

                    if float(each['current_total_discounts']) != 0.00:
                        discount_lines = each['current_total_discounts_set']
                        product_id = self.env.ref(
                            'shopify_odoo_connector.product_shopify_order_discount')
                        discount_dict = {
                            'product_id': product_id.id,
                            'price_unit': -float(
                                discount_lines['shop_money']['amount']),
                            'product_uom_qty': 1,
                            'tax_id': None,
                            'order_id': so.id,
                        }
                        # line_vals_list.append(discount_dict)

                        discount_code = each['discount_codes'][0]['code']

                        discount_reason =self.env['discount.reason'].search([('shopify_discount', '=',discount_code)], limit=1)
                        so.discount_id = discount_reason.id
                    if line_vals_list:
                        # no local except: a failure here must reach the
                        # order-level handler, which rolls the order back
                        # and logs why
                        new_lines = self.env['sale.order.line'].sudo().create(
                            line_vals_list)
                    else:
                        new_lines = self.env['sale.order.line'].browse()
                    # if not wizard.draft:
                    so.action_confirm()
                    # Force Shopify prices onto the lines.
                    # Both create() and action_confirm() trigger Odoo 18's
                    # _compute_price_unit which overwrites price_unit with the
                    # pricelist price.  The only reliable fix is to:
                    #   1. flush_all() so all pending ORM writes reach the DB,
                    #   2. patch price_unit / discount directly via SQL,
                    #   3. remove those fields from the recompute queue so
                    #      the engine does not re-run the compute method,
                    #   4. invalidate the ORM cache and recompute monetary
                    #      totals from the corrected price.
                    if new_lines and line_vals_list:
                        self.env.flush_all()
                        cr = self.env.cr
                        for sol, lv in zip(new_lines, line_vals_list):
                            price = float(lv.get('price_unit') or 0)
                            disc = float(lv.get('discount') or 0)
                            cr.execute(
                                "UPDATE sale_order_line "
                                "SET price_unit = %s, discount = %s "
                                "WHERE id = %s",
                                (price, disc, sol.id),
                            )
                        # Drop price_unit/discount from the pending recompute set
                        sol_model = self.env['sale.order.line']
                        pf = sol_model._fields.get('price_unit')
                        df = sol_model._fields.get('discount')
                        tocompute = getattr(
                            getattr(self.env, 'all', None), 'tocompute', {})
                        line_ids = set(new_lines.ids)
                        for fld in (pf, df):
                            if fld and fld in tocompute:
                                tocompute[fld] -= line_ids
                        # Refresh ORM cache and recompute monetary totals
                        new_lines.invalidate_recordset(
                            ['price_unit', 'discount'])
                        new_lines.sudo()._compute_amount()
            except Exception as error:
                self._log_confirmed_order_failure(
                    savepoint, each, instance, error)
                continue
            finally:
                if not savepoint.closed:
                    try:
                        savepoint.close(rollback=False)
                    except Exception as error:
                        self._log_confirmed_order_failure(
                            savepoint, each, instance, error)

    # ------------------------------------------------------------------
    # order payload helpers
    # ------------------------------------------------------------------
    _ORDER_LIST_KEYS = ('line_items', 'tax_lines', 'discount_applications',
                        'discount_codes', 'refunds', 'shipping_lines',
                        'payment_gateway_names')

    def _normalize_shopify_order(self, order, instance):
        """Fill the keys the importer reads but a payload may leave out
        (API test orders, trimmed webhooks), so a missing key is never a
        KeyError. Values Shopify did send are left untouched."""
        for key in self._ORDER_LIST_KEYS:
            if order.get(key) is None:
                order[key] = []
        for key in ('customer', 'shipping_address', 'billing_address',
                    'note', 'fulfillment_status', 'financial_status'):
            order.setdefault(key, None)
        if not order.get('name'):
            order['name'] = '#%s' % order.get('id')
        if not order.get('number'):
            order['number'] = order['name']
        if not order.get('created_at'):
            order['created_at'] = fields.Datetime.now().isoformat() + 'Z'
        if not order.get('currency'):
            order['currency'] = instance.company_id.currency_id.name
        if order.get('current_total_discounts') in (None, ''):
            order['current_total_discounts'] = '0'
        order.setdefault('current_total_discounts_set', {})
        for item in order['line_items']:
            if not isinstance(item, dict):
                continue
            for key in ('discount_allocations', 'tax_lines'):
                if item.get(key) is None:
                    item[key] = []
            item.setdefault('taxable', bool(item['tax_lines']))
            for key in ('sku', 'variant_id', 'product_id', 'title', 'name'):
                item.setdefault(key, None)
            item.setdefault('quantity', 1)
            item.setdefault('price', '0')
        return order

    @staticmethod
    def _shopify_person_name(data):
        data = data or {}
        name = ' '.join(part for part in (
            (data.get('first_name') or '').strip(),
            (data.get('last_name') or '').strip()) if part)
        return name or (data.get('name') or '').strip()

    def _shopify_address_vals(self, address):
        """res.partner address values from a Shopify address dict."""
        address = address or {}
        if not address:
            return {}
        country = self.env['res.country'].sudo()
        if address.get('country_code'):
            country = country.search(
                [('code', '=ilike', address['country_code'])], limit=1)
        if not country and address.get('country'):
            country = self.env['res.country'].sudo().search(
                [('name', '=ilike', address['country'])], limit=1)
        state = self.env['res.country.state'].sudo()
        if address.get('province'):
            domain = [('name', '=ilike', address['province'])]
            if country:
                domain.append(('country_id', '=', country.id))
            state = state.search(domain, limit=1)
        return {
            'street': address.get('address1') or False,
            'street2': address.get('address2') or False,
            'city': address.get('city') or False,
            'zip': address.get('zip') or False,
            'country_id': country.id or False,
            'state_id': state.id or False,
        }

    def _fetch_shopify_customer(self, instance, customer_ref):
        """Customer record from the Shopify API, or {} when it cannot be
        read (test order, deleted customer, network error)."""
        try:
            response = requests.get(
                'https://%s/admin/api/%s/customers/%s.json' % (
                    instance.shop_name, instance.version, customer_ref),
                headers=instance._get_shopify_headers(), timeout=30)
            if response.status_code == 200:
                return response.json().get('customer') or {}
            _logger.info('Shopify customer %s not readable (HTTP %s); using '
                         'the order data', customer_ref, response.status_code)
        except Exception as error:  # noqa: BLE001
            _logger.info('Shopify customer %s not readable (%s); using the '
                         'order data', customer_ref, error)
        return {}

    def _find_or_create_order_partner(self, order, instance):
        """Return the res.partner for a Shopify order, creating it when no
        existing customer matches.

        Match order: Shopify customer id (shopify.sync) -> email -> phone.
        When nothing matches, a partner is created from the Shopify customer
        (API), falling back to the order's own customer / billing / shipping
        data, so an order is never refused for lacking a customer.
        """
        Partner = self.env['res.partner'].sudo().with_context(
            shopify_no_export=True)
        company_domain = [('company_id', 'in',
                           [instance.company_id.id, False])]
        customer = order.get('customer') or {}
        billing = order.get('billing_address') or {}
        shipping = order.get('shipping_address') or {}
        customer_ref = customer.get('id')
        customer_ref = str(customer_ref) if customer_ref else False

        partner = Partner.browse()
        if customer_ref:
            partner = Partner.search([
                ('shopify_sync_ids.shopify_customer_ref', '=', customer_ref),
                ('shopify_sync_ids.instance_id', '=', instance.id),
            ] + company_domain, limit=1)
            if partner:
                return partner

        details = dict(customer)
        if customer_ref:
            details.update({key: value for key, value in
                            self._fetch_shopify_customer(
                                instance, customer_ref).items()
                            if value})

        email = (details.get('email') or order.get('email')
                 or order.get('contact_email') or '').strip()
        phone = (details.get('phone') or order.get('phone')
                 or billing.get('phone') or shipping.get('phone') or '').strip()
        if email:
            partner = Partner.search(
                [('email', '=ilike', email)] + company_domain, limit=1)
        if not partner and phone:
            partner = Partner.search(
                ['|', ('mobile', '=', phone), ('phone', '=', phone)]
                + company_domain, limit=1)

        created = False
        if not partner:
            address = (details.get('default_address')
                       or (details.get('addresses') or [None])[0]
                       or billing or shipping)
            name = (self._shopify_person_name(details)
                    or self._shopify_person_name(billing)
                    or self._shopify_person_name(shipping)
                    or email or phone
                    or _('Shopify customer %s') % order.get('name'))
            partner_vals = dict(self._shopify_address_vals(address), **{
                'name': name,
                'email': email or False,
                'mobile': phone or False,
                'shopify_instance_id': instance.id,
                'synced_customer': bool(customer_ref),
                'shopify_customer_ref': customer_ref,
                'company_id': instance.company_id.id,
            })
            partner = Partner.create(partner_vals)
            created = True

        if customer_ref and not partner.shopify_sync_ids.filtered(
                lambda sync: sync.instance_id == instance
                and sync.shopify_customer_ref == customer_ref):
            self.env['shopify.sync'].sudo().create({
                'instance_id': instance.id,
                'shopify_customer_ref': customer_ref,
                'customer_id': partner.id,
            })
        if created:
            self.env['log.message'].sudo().create([{
                'name': 'Customer %s created from Shopify order %s' % (
                    partner.name, order.get('name')),
                'shopify_instance_id': instance.id,
                'model': 'res.partner',
            }])
        return partner

    def _log_confirmed_order_failure(self, savepoint, order, instance, error):
        """Roll one Shopify order back and record why it failed.

        Written as a `sale.order` log.message so the confirmed-order API can
        return it as the rejection reason."""
        if not savepoint.closed:
            savepoint.close(rollback=True)
        name = order.get('name') or order.get('id')
        _logger.exception('Shopify order %s could not be imported', name)
        message = str(error) or error.__class__.__name__
        self.env['log.message'].sudo().create([{
            'name': 'Creation of order %s failed: %s' % (name, message),
            'shopify_instance_id': instance.id,
            'model': 'sale.order',
        }])

    def import_draft_orders_from_shopify(self, shopify_orders, instance):
        """ Method to import draft orders from shopify to odoo.
             job evokes this method for creating draft orders in odoo.
            shopify_orders(list):list of dictionary with order values
        """
        shopify_instance = instance
        store_name = instance.shop_name
        version = instance.version
        headers = instance._get_shopify_headers()
        vals = {}
        for each in shopify_orders:
            shopify_id = each['id']
            existing_order = self.env['sale.order'].search(
                [('shopify_sync_ids.shopify_order_ref', '=', shopify_id)])
            if not existing_order and each['status'] != 'completed':
                state_id = None
                country_id = None
                if 'customer' in each.keys() and each['customer'] is not None:
                    customer_id = each['customer'].get('id')
                    partner_id = self.env['res.partner'].sudo().search(
                        [('shopify_sync_ids.shopify_customer_ref', '=',
                          customer_id),
                         ('shopify_sync_ids.instance_id', '=',
                          shopify_instance.id),
                         ('company_id', 'in',
                          [shopify_instance.company_id.id, False])],
                        limit=1).id
                    if not partner_id:
                        customer_url = ("https://%s/admin/api/%s/"
                                        "customers/%s.json") % (
                                           store_name, version, customer_id)
                        response = requests.request("GET", customer_url,
                                                    headers=headers,
                                                    data=[])
                        customer_response = response.json()
                        customer_vals = {}
                        customer = customer_response['customer']
                        if customer['addresses']:
                            country_id = self.env[
                                'res.country'].sudo().search(
                                [('name', '=',
                                  customer['addresses'][0]['country'])])
                            state_id = self.env[
                                'res.country.state'].sudo().search(
                                [('name', '=',
                                  customer['addresses'][0]['province'])])
                            customer_vals = {
                                'street': customer['addresses'][0]['address1'],
                                'street2': customer['addresses'][0]['address2'],
                                'city': customer['addresses'][0]['city'],
                                'country_id': country_id.id if country_id
                                else False,
                                'state_id': state_id.id if state_id
                                else False,
                                'zip': customer['addresses'][0]['zip'],
                            }
                        if (customer['first_name'] and
                                not customer['last_name']):
                            customer_vals['name'] = customer['first_name']
                        if (customer['last_name'] and
                                not customer['first_name']):
                            customer_vals['name'] = customer['last_name']
                        if customer['first_name'] and customer['last_name']:
                            customer_vals['name'] = (customer['first_name'] +
                                                     ' ' +
                                                     customer['last_name'])
                        customer_vals['email'] = customer['email']
                        customer_vals['phone'] = customer['phone']
                        customer_vals['shopify_customer_ref'] = customer['id']
                        customer_vals[
                            'shopify_instance_id'] = shopify_instance.id
                        customer_vals['synced_customer'] = True
                        customer_vals[
                            'company_id'] = shopify_instance.company_id.id
                        partner_id = self.env['res.partner'].sudo().create(
                            customer_vals).id
                        partner_ = self.env['res.partner'].sudo().browse(
                            partner_id)
                        partner_.shopify_sync_ids.sudo().create({
                            'instance_id': instance.id,
                            'shopify_customer_ref': customer_id,
                            'customer_id': partner_id,
                        })
                    vals["partner_id"] = partner_id
                    if each['shipping_address']:
                        partner_creation_data = {
                            "name": each['shipping_address'][
                                'first_name'] if 'first_name' in each[
                                'shipping_address'].keys() else
                            self.env['res.partner'].sudo().browse(
                                partner_id).name,
                            "street": each['shipping_address'][
                                'address1'] if 'address1' in each[
                                'shipping_address'].keys() else '',
                            "street2": each['shipping_address'][
                                'address2'] if 'address2' in each[
                                'shipping_address'].keys() else '',
                            "city": each['shipping_address']['city'] if
                            'city' in each['shipping_address'].keys() else '',
                            "phone": each['shipping_address']['phone'] if
                            'phone' in each['shipping_address'].keys() else '',
                            "zip": each['shipping_address']['zip'] if
                            'zip' in each['shipping_address'].keys() else '',
                            "parent_id": partner_id,
                            "type": 'delivery'}
                        if state_id:
                            partner_creation_data["state_id"] = state_id.id
                        if country_id:
                            partner_creation_data["country_id"] = country_id.id
                        shipping_child_id = self.env[
                            'res.partner'].sudo().create(
                            partner_creation_data).id
                        vals['partner_shipping_id'] = shipping_child_id
                    if each['billing_address']:
                        country_id = self.env['res.country'].search([
                            ('name', '=', each['shipping_address']['country'])
                        ])
                        state_id = self.env['res.country.state'].search([
                            ('name', '=', each['shipping_address']['province'])
                        ])
                        invoice_creation_data = {
                            "name": each['shipping_address'][
                                'first_name'] if 'first_name' in each[
                                'shipping_address'].keys() else
                            self.env['res.partner'].sudo().browse(
                                partner_id).name,
                            "street": each['shipping_address'][
                                'address1'] if 'address1' in each[
                                'shipping_address'].keys() else '',
                            "street2": each['shipping_address'][
                                'address2'] if 'address2' in each[
                                'shipping_address'].keys() else '',
                            "city": each['shipping_address']['city'] if
                            'city' in each['shipping_address'].keys() else '',
                            "phone": each['shipping_address']['phone'] if
                            'phone' in each['shipping_address'].keys() else '',
                            "zip": each['shipping_address']['zip'] if
                            'zip' in each['shipping_address'].keys() else '',
                            "parent_id": partner_id,
                            "type": 'invoice'}
                        if state_id:
                            invoice_creation_data["state_id"] = state_id.id
                        if country_id:
                            invoice_creation_data["country_id"] = country_id.id
                        billing_child_id = self.env[
                            'res.partner'].sudo().create(
                            invoice_creation_data).id
                        vals['partner_invoice_id'] = billing_child_id
                else:
                    self.env['log.message'].sudo().create([{
                        'name': 'Creation draft order : ' + each[
                            'name'] + ' is not processed. Order does not'
                                      ' contain a customer.',
                        'shopify_instance_id': instance.id,
                        'model': 'sale.order',
                    }])
                    continue
                if each['tax_lines']:
                    tax = each['tax_lines'][0]['rate']
                    tax_group = each['tax_lines'][0]["title"]
                    taxes = tax * 100
                    tax_name = self.env[
                        'account.tax'].search(
                        [('amount', '=', taxes),
                         ('type_tax_use', '=', 'sale'),
                         ('company_id', '=', instance.company_id.id)],
                        limit=1).id
                    if not tax_name:
                        tax_group_id = self.env['account.tax.group'].create(
                            {'name': tax_group})
                        tax_name = self.env['account.tax'].create(
                            [{'name': tax_group + str(taxes) + '%',
                              'type_tax_use': 'sale',
                              'amount_type': 'percent',
                              'tax_group_id': tax_group_id.id,
                              'amount': taxes,
                              }])
                else:
                    tax_name = None
                vals["date_order"] = str(odoo.fields.Datetime.to_string(
                    dateutil.parser.parse(each['created_at']).astimezone(
                        pytz.utc)))
                vals["shopify_order_ref"] = each['id']
                vals["name"] = each['name']
                vals['shopify_instance_id'] = shopify_instance.id
                sale_order = self.env['sale.order']
                so = sale_order.create(vals)
                so.shopify_sync_ids.sudo().create({
                    'instance_id': instance.id,
                    'shopify_order_ref': each['id'],
                    'shopify_order_name': each['name'],
                    'shopify_order_number': each['id'],
                    'order_status': each['status'],
                    'order_id': so.id,
                    'synced_order': True,
                })
                so.shopify_order_ref = each['id']
                currency = self.env['res.currency'].sudo().search(
                    [
                        ('name', 'ilike', each['currency']),
                        ('active', 'in', [False, True]),
                    ])
                if currency and not currency.active:
                    currency.sudo().write({'active': True})
                line_vals_list = []
                for line in each['line_items']:
                    discount = 0.0
                    if line['applied_discount']:
                        discount = line['applied_discount']['amount']
                    product_id = self.env['product.product'].sudo().search(
                        [('shopify_product', '=', line['product_id']), (
                            'shopify_sync_ids.instance_id', '=',
                            shopify_instance.id),
                         ('company_id', 'in',
                          [shopify_instance.company_id.id, False])])
                    if line['variant_id']:
                        product_id = product_id.search([
                            ('shopify_variant', '=', line['variant_id']),
                        ])
                    if not product_id:
                        product = line['product_id']
                        product_response = self.env[
                            'sync.product'].create_product_by_id(
                            shopify_instance, store_name, version, product)
                        if 'errors' in product_response:
                            self.env['log.message'].sudo().create([{
                                'name': ' Creation of product  order : ' +
                                        each['name'] + ' with product id:  ' +
                                        str(line['id']) + ' and name:  ' +
                                        line['title'] + '  is not processed.'
                                                        ' Product does not '
                                                        'exists in Shopify.',
                                'shopify_instance_id': instance.id,
                                'model': 'sale.order',
                            }])
                            continue
                        if line['variant_id']:
                            product_id = self.env['product.product'].search([
                                ('shopify_variant', '=', line['variant_id']),
                            ])
                    str_list = []
                    line_vals = {
                        'product_id': product_id.id,
                        'price_unit': line['price'],
                        'name': ' ',
                        'product_uom_qty': line['quantity'],
                        'currency_id': currency.id,
                        'discount': (float(discount) / float(
                            line['price']) * 100) / float(line['quantity'])
                        if discount else 0,
                        'tax_id': [
                            (6, 0, tax_name.ids)] if tax_name else False,
                        'shopify_line_ref': line['id'],
                        'shopify_instance_id': shopify_instance.id,
                        'shopify_taxable': line['taxable'],
                        'shopify_tax_amount': float(
                            line['tax_lines'][0]['price']) if
                        line['tax_lines'] else 0.0,
                        'shopify_discount_amount': float(
                            line['applied_discount']['amount']) if line[
                            'applied_discount'] else 0.0,
                        'shopify_line_item_discount': float(
                            line['applied_discount']['amount']) if line[
                            'applied_discount'] else 0.0,
                        'shopify_discount_code': ','.join(str_list),
                        'order_id': so.id,
                        'company_id': shopify_instance.company_id.id,
                    }
                    if 'refunds' in each.keys():
                        for refunds in each['refunds']:
                            for refund_line in refunds['refund_line_items']:
                                if refund_line['line_item_id'] == line['id']:
                                    line_vals['product_uom_qty'] -= \
                                        refund_line['quantity']
                    line_vals_list.append(line_vals)
                if each['shipping_line']:
                    shipping_line = each['shipping_line']
                    shipping_product_id = self.env.ref(
                        'shopify_odoo_connector.product_shopify_shipping_cost')
                    line_vals = {
                        'product_id': shipping_product_id.id,
                        'name': shipping_line['title'] if shipping_line[
                            'title'] else shipping_product_id.name,
                        'price_unit': shipping_line['price'],
                        'product_uom_qty': 1,
                        'shopify_line_ref': '',
                        'tax_id': False,
                        'order_id': so.id,
                        'shopify_instance_id': shopify_instance.id,
                        'company_id': shopify_instance.company_id.id,
                    }
                    line_vals_list.append(line_vals)
                if each['applied_discount']:
                    discount_line = each['applied_discount']
                    discount_product_id = self.env.ref(
                        'shopify_odoo_connector.product_shopify_order_discount')
                    dis_line_vals = {
                        'product_id': discount_product_id.id,
                        'name': discount_line['title'] if discount_line[
                            'title'] else
                        discount_product_id.name + " : " + discount_line[
                            'value_type'] + " - " + discount_line['value'],
                        'price_unit': -float(discount_line['amount']),
                        'order_id': so.id,
                        'tax_id': None,
                    }
                    line_vals_list.append(dis_line_vals)
                sale_order_line = self.env['sale.order.line']
                sale_order_line.create(line_vals_list)

    def export_orders_to_shopify(self, lists, instance):
        """ Method to export orders from odoo to shopify.
             job evokes this method to export odoo orders.
            sale_order(list):list of dictionary with order values.
        """
        store_name = instance.shop_name
        version = instance.version
        sale_order = self.env['sale.order'].sudo().search([('id', 'in', lists)])
        order_url = "https://%s/admin/api/%s/draft_orders.json" % (
            store_name, version)
        headers = instance._get_shopify_headers()
        for order in sale_order:
            instance_ids = order.shopify_sync_ids.mapped('instance_id.id')
            if instance.id not in instance_ids:
                line_items = []
                for line in order.order_line:
                    line_vals = {
                        "title": line.product_id.name,
                        "price": line.price_unit,
                        "quantity": int(line.product_uom_qty),
                    }
                    line_items.append(line_vals)
                payload = json.dumps({
                    "draft_order": {
                        "line_items": line_items,
                        "email": order.partner_id.email,
                        "use_customer_default_address": True
                    }
                })
                response = requests.request("POST", order_url,
                                            headers=headers,
                                            data=payload)
                if response.status_code == 201:
                    response_rec = response.json()
                    response_order_id = response_rec['draft_order']['id']
                    response_status = response_rec['draft_order']['status']
                    response_name = response_rec['draft_order']['name']
                    order.shopify_sync_ids.sudo().create({
                        'instance_id': instance.id,
                        'shopify_order_ref': response_order_id,
                        'shopify_order_name': response_name,
                        'shopify_order_number': response_order_id,
                        'order_status': response_status,
                        'order_id': order.id,
                        'synced_order': True,
                    })
                    order.shopify_order_ref = response_order_id

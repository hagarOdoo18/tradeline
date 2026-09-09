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
from datetime import timedelta
from odoo import api, models, fields, _
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

# Shopify's maximum page size for REST list endpoints. The old code left it
# unset on the first page (so Shopify's default of 50 applied), which needed
# five times more round-trips than necessary to walk the same orders.
SHOPIFY_PAGE_LIMIT = 250
# No request may hang forever: a stalled connection here blocks the cron slot.
SHOPIFY_TIMEOUT = 30
# Shopify stamps updated_at server-side, so an order can be written a few
# seconds either side of the moment recorded as "synced up to here". Asking
# again for a short overlap costs one page and closes that gap for good.
SHOPIFY_SYNC_OVERLAP = timedelta(minutes=10)
# A malformed Link header must not turn pagination into an unbounded loop.
SHOPIFY_MAX_PAGES = 200


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

    @staticmethod
    def _shopify_next_page_url(response):
        """Return the next page's URL from a Shopify Link header, or None.

        Shopify already hands back the complete next URL, cursor and page
        size included. Taking it verbatim replaces the old greedy
        `page_info=(.*)>` extraction (which broke as soon as the header
        carried more than one link) and, more importantly, preserves the
        filters of the first request — a page_info cursor may not be
        combined with any other filter, so the URL must be reused as given.
        """
        for link in (response.headers.get('link') or '').split(','):
            if 'rel="next"' not in link:
                continue
            match = re.search(r'<([^>]+)>', link)
            if match:
                return match.group(1)
        return None

    def sync_confirmed_orders(self, instance, ref):
        """Queue jobs that import confirmed orders from Shopify.

        Only orders updated since the instance's `order_last_synced`
        watermark are fetched. Without that filter every run walked the whole
        open-order history starting at page 1 and queued a job per page; at
        the cron's five-minute interval that re-imported every order in the
        store 288 times a day and the job queue could never drain. The
        watermark field already existed on shopify.configuration — it was
        simply never written or read.

        The watermark only advances when the walk reached the last page, so
        an interrupted run is retried from where it left off rather than
        silently skipping the orders it never saw.
        """
        model = self.env['ir.model'].search([('model', '=', "sale.order.sync")])
        headers = instance._get_shopify_headers()
        started_at = fields.Datetime.now()
        watermark = instance.order_last_synced

        url = "https://%s/admin/api/%s/orders.json" % (
            instance.shop_name, instance.version)
        # `status` is deliberately left unset so Shopify's default of
        # status=open still applies -- same set of orders as before, just
        # restricted to the ones that actually changed.
        params = {'limit': SHOPIFY_PAGE_LIMIT}
        if watermark:
            # Odoo stores naive UTC. Sending it without an offset would let
            # Shopify read it in the shop's own timezone and silently shift
            # the window by hours, so stamp it explicitly.
            params['updated_at_min'] = (
                watermark - SHOPIFY_SYNC_OVERLAP
            ).replace(tzinfo=pytz.utc).isoformat()
        else:
            _logger.info(
                'Shopify order sync: no watermark on instance %s, walking '
                'the full order history once.', instance.display_name)

        complete = False
        pages = 0
        queued_orders = 0
        for _page in range(SHOPIFY_MAX_PAGES):
            response = requests.get(url, headers=headers, params=params,
                                    timeout=SHOPIFY_TIMEOUT)
            response.raise_for_status()
            orders = response.json().get('orders') or []
            pages += 1
            if orders:
                self.env['job.cron'].sudo().create([{
                    'model_id': model.id,
                    'function': "import_confirmed_orders_from_shopify",
                    'data': orders,
                    'instance_id': instance.id,
                    'wizard': ref,
                }])
                queued_orders += len(orders)
            url = self._shopify_next_page_url(response)
            if not url:
                complete = True
                break
            # the cursor URL already carries limit and page_info, and
            # Shopify rejects a page_info request that repeats the filters
            params = None

        if complete:
            instance.sudo().order_last_synced = started_at
        else:
            _logger.warning(
                'Shopify order sync: stopped after %s pages on instance %s '
                'without reaching the end; the watermark was not advanced '
                'and the next run resumes from %s.',
                SHOPIFY_MAX_PAGES, instance.display_name, watermark)
        _logger.info(
            'Shopify order sync: %s page(s), %s order(s) queued for '
            'instance %s.', pages, queued_orders, instance.display_name)

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
            loc = location_model.search([
                ('name', '=', str(location_id)),
                ('instance_id', '=', instance.id),
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

        # One grouped read for every candidate instead of a stock.quant
        # search per candidate: an order line with three siblings used to
        # cost four separate quant scans.
        candidates = product | siblings
        free_by_product = dict.fromkeys(candidates.ids, 0.0)
        for prod, quantity, reserved in self.env['stock.quant'].sudo()._read_group(
                [('product_id', 'in', candidates.ids),
                 ('company_id', '=', company_id),
                 ('location_id', 'child_of', location.id)],
                ['product_id'],
                ['quantity:sum', 'reserved_quantity:sum']):
            free_by_product[prod.id] = (quantity or 0.0) - (reserved or 0.0)

        # Keep the resolved product when it can cover the line itself.
        if free_by_product.get(product.id, 0.0) >= qty_needed:
            return product

        # Otherwise take the sibling with the most free stock that still
        # covers the ordered quantity.
        scored = [(candidate, free_by_product.get(candidate.id, 0.0))
                  for candidate in siblings]
        scored.sort(key=lambda item: item[1], reverse=True)
        for candidate, free in scored:
            if free >= qty_needed:
                return candidate
        return product

    def _confirmed_order_warehouse_context(self, code, instance, cache):
        """Resolve — once per distinct shipping code — everything an order
        derives from its fulfilment location.

        Warehouse, branch, sales team, salesperson and invoice journal were
        five separate searches on every order, for values that depend only on
        the code. Memoising them per page collapses that to five searches per
        distinct location.
        """
        key = str(code or '')
        if key in cache:
            return cache[key]
        warehouse = self._get_shopify_order_warehouse(code, instance)
        branch_id = warehouse.branch_id.id if warehouse else False
        team = self.env['crm.team'].search(
            [('branch_id', '=', branch_id),
             ('company_id', '=', self.env.company.id)], limit=1)
        if branch_id != 88:
            # `limit=1` matters: without it a branch with more than one user
            # made `.id` raise a singleton error, which the bare except below
            # swallowed -- the order then silently never imported at all.
            user_id = self.env['res.users'].search(
                [('branch_id', '=', branch_id)], limit=1).id
        else:
            user_id = 66
        journal = self.env['account.journal'].search(
            [('type', '=', 'sale'), ('branch_id', '=', branch_id)], limit=1)
        ctx = {
            'warehouse': warehouse,
            'branch_id': branch_id,
            'team_id': team.id if team else False,
            'user_id': user_id if user_id else False,
            'journal_id': journal.id,
        }
        cache[key] = ctx
        return ctx

    def _confirmed_order_currency(self, code, cache):
        """Resolve a Shopify currency code to an active res.currency, once
        per distinct code instead of once per order."""
        key = code or ''
        if key in cache:
            return cache[key]
        currency = self.env['res.currency'].sudo().search(
            [('name', 'ilike', code), ('active', 'in', [False, True])],
            limit=1)
        if currency and not currency.active:
            currency.sudo().write({'active': True})
        cache[key] = currency
        return currency

    def _confirmed_order_tax(self, each, instance, cache):
        """Resolve (creating it if needed) the sale tax for an order, once
        per distinct rate instead of once per order."""
        if not each.get('tax_lines'):
            return None
        rate = each['tax_lines'][0]['rate']
        tax_group = each['tax_lines'][0]['title']
        key = (rate, tax_group)
        if key in cache:
            return cache[key]
        taxes = rate * 100
        tax_name = self.env['account.tax'].search(
            [('name', '=', '14%'),
             ('type_tax_use', '=', 'sale'),
             ('company_id', '=', instance.company_id.id)], limit=1)
        if tax_name:
            cache[key] = tax_name
            return tax_name
        # A tax created here belongs to the savepoint of the order being
        # imported: if that order rolls back, the record goes with it. Caching
        # it would hand every later order in the page a dangling id, so it is
        # deliberately not cached -- the next order with this rate re-runs the
        # search above, which sees the row while the transaction still holds
        # it and finds nothing once it has been rolled back.
        tax_group_id = self.env['account.tax.group'].create(
            {'name': tax_group})
        return self.env['account.tax'].create(
            [{'name': tax_group + str(taxes) + '%',
              'type_tax_use': 'sale',
              'amount_type': 'percent',
              'tax_group_id': tax_group_id.id,
              'amount': taxes,
              }])

    def import_confirmed_orders_from_shopify(self, shopify_orders, instance,
                                             ref):
        """ Method to import confirmed orders from shopify to odoo.
             job evokes this method for creating confirmed orders in odoo.

            get_shopify_orders(list):list of dictionary with orders values.

        Which orders already exist is answered by one query for the whole
        page. It used to be a `sale.order` search joined through the
        shopify.sync one2many for every single order — and since the fetch
        re-sent the entire order history on every run, that lookup was
        almost always the only work an order needed.
        """
        if not shopify_orders:
            return
        wizard = self.env['sale.order.sync'].sudo().browse(ref)
        shopify_instance = instance
        store_name = instance.shop_name
        version = instance.version
        headers = instance._get_shopify_headers()

        shopify_ids = [str(order['id']) for order in shopify_orders
                       if order.get('id')]
        existing_refs = set()
        if shopify_ids:
            existing_refs = set(self.env['shopify.sync'].sudo().search(
                [('shopify_order_ref', 'in', shopify_ids),
                 ('order_id', '!=', False)]
            ).mapped('shopify_order_ref'))

        # per-page caches for lookups whose result depends only on a key that
        # repeats across orders
        warehouse_cache = {}
        currency_cache = {}
        tax_cache = {}
        sales_rep = self.env['sales.rep'].search(
            [('company_id', '=', instance.company_id.id),
             ('is_online', '=', True)], limit=1)
        imported = 0
        failed = 0

        for each in shopify_orders:
            # Build a fresh vals dict for every order. Odoo's sale.order
            # create() writes the generated sequence back into vals
            # (vals['name'] = 'S00xxx'); a shared dict would keep that name
            # and reuse the same sequence for every subsequent order. A fresh
            # dict also prevents fields (shipping/billing, taxes, warehouse)
            # from leaking between orders.
            vals = {}
            shopify_id = each['id']
            try:
                # One savepoint per order: a database error on a single order
                # rolls that order back and leaves the transaction usable for
                # the rest of the page. Without it the first SQL failure put
                # the transaction in an aborted state and every remaining order
                # failed with it, so one bad order lost the whole page.
                with self.env.cr.savepoint():
                    if str(shopify_id) not in existing_refs:
                        if each['customer']:
                            customer_id = each['customer'].get('id')
                            if (each['customer']['first_name'] or
                                    each['customer']['last_name']):
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
                                              customer['addresses'][0]['country'])
                                             ])
                                        state_id = self.env[
                                            'res.country.state'].sudo().search(
                                            [('name', '=',
                                              customer['addresses'][0]['province'])])
                                        customer_vals = {
                                            'street': customer['addresses'][0][
                                                'address1'],
                                            'street2': customer['addresses'][0][
                                                'address2'],
                                            'city': customer['addresses'][0]['city'],
                                            'country_id': country_id.id if
                                            country_id else False,
                                            'state_id': state_id.id if
                                            state_id else False,
                                            'zip': customer['addresses'][0]['zip'],
                                        }
                                    if (customer['first_name'] and
                                            not customer['last_name']):
                                        customer_vals['name'] = customer['first_name']
                                    if (customer['last_name'] and
                                            not customer['first_name']):
                                        customer_vals['name'] = customer['last_name']
                                    if customer['first_name'] and customer['last_name']:
                                        customer_vals['name'] = (customer['first_name']
                                                                 + ' '
                                                                 + customer['last_name'])
                                    customer_vals['email'] = customer['email']
                                    customer_vals['phone'] = customer['phone']
                                    customer_vals['shopify_customer_ref'] = customer[
                                        'id']
                                    customer_vals[
                                        'shopify_instance_id'] = shopify_instance.id
                                    customer_vals['synced_customer'] = True
                                    customer_vals[
                                        'company_id'] = shopify_instance.company_id.id
                                    partner_id = self.env['res.partner'].sudo().create(
                                        customer_vals).id
                                    partner_ = self.env['res.partner'].browse(
                                        partner_id)
                                    partner_.shopify_sync_ids.sudo().create({
                                        'instance_id': instance.id,
                                        'shopify_customer_ref': customer_id,
                                        'customer_id': partner_id,
                                    })
                                vals["partner_id"] = partner_id
                                if each['shipping_address']:
                                    county_id = self.env['res.country'].search([
                                        ('name', '=',
                                         each['shipping_address']['country'])
                                    ])
                                    state_id = self.env['res.country.state'].search([
                                        ('name', '=',
                                         each['shipping_address']['province'])
                                    ],limit=1)
                                    shipping_child_id = self.env[
                                        'res.partner'].sudo().create([
                                        {"name": each['shipping_address'][
                                            'first_name'] if each['shipping_address'][
                                            'first_name'] else '',
                                         "street": each['shipping_address'][
                                             'address1'] if each['shipping_address'][
                                             'address1'] else '',
                                         "street2": each['shipping_address'][
                                             'address2'] if each['shipping_address'][
                                             'address2'] else '',
                                         "city": each['shipping_address']['city'] if
                                         each['shipping_address']['city'] else '',
                                         "state_id": state_id.id or None,
                                         "phone": each['shipping_address']['phone'] if
                                         each['shipping_address']['phone'] else None,
                                         "zip": each['shipping_address']['zip'] if
                                         each['shipping_address']['zip'] else '',
                                         "country_id": county_id.id or None,
                                         "parent_id": partner_id,
                                         "type": 'delivery',
                                         }]).id
                                    vals['partner_shipping_id'] = shipping_child_id
                                if each['billing_address'] and each['shipping_address'] :

                                    county_id = self.env['res.country'].search([
                                        ('name', '=',
                                         each['shipping_address']['country'])
                                    ])
                                    state_id = self.env['res.country.state'].search([
                                        ('name', '=',
                                         each['shipping_address']['province'])
                                    ],limit=1)
                                    billing_child_id = self.env[
                                        'res.partner'].sudo().create(
                                        [{
                                            "name": each['billing_address'][
                                                'first_name'] if
                                            each['billing_address'][
                                                'first_name'] else '',
                                            "street": each['billing_address'][
                                                'address1'] if
                                            each['billing_address'][
                                                'address1'] else '',
                                            "street2": each['billing_address'][
                                                'address2'] if each['billing_address'][
                                                'address2'] else '',
                                            "city": each['billing_address']['city'] if
                                            each['billing_address']['city'] else '',
                                            "state_id": state_id.id or None,
                                            "phone": each['billing_address']['phone'] if
                                            each['billing_address'][
                                                'phone'] else None,
                                            "zip": each['billing_address']['zip'] if
                                            each['billing_address']['zip'] else '',
                                            "country_id": county_id.id or None,
                                            "parent_id": partner_id,
                                            "type": 'invoice'}]).id
                                    vals['partner_invoice_id'] = billing_child_id
                            else:
                                self.env['log.message'].sudo().create([{
                                    'name': ' Creation of order : ' + each[
                                        'name'] + ' is not processed. Customer does'
                                                  ' not have a name.',
                                    'shopify_instance_id': instance.id,
                                    'model': 'sale.order',
                                }])
                                continue
                        else:
                            self.env['log.message'].sudo().create(
                                [{
                                    'name': 'Creation order : ' + each[
                                        'name'] + ' is not processed. Order does not '
                                                  'contain a customer.',
                                    'shopify_instance_id': instance.id,
                                    'model': 'sale.order',
                                }])
                            continue
                        tax_name = self._confirmed_order_tax(
                            each, instance, tax_cache)
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
                        # An order with no shipping line used to raise IndexError
                        # here, which the bare except at the bottom swallowed --
                        # the order then silently never imported.
                        line = shipping_lines[0] if shipping_lines else {}

                        warehouse_ctx = self._confirmed_order_warehouse_context(
                            line.get('code'), shopify_instance, warehouse_cache)
                        order_warehouse = warehouse_ctx['warehouse']
                        vals['warehouse_id'] = (
                            order_warehouse.id if order_warehouse else False)
                        vals['team_id'] = warehouse_ctx['team_id']
                        vals['sales_rep_id'] = sales_rep.id if sales_rep else False
                        vals['branch_id'] = warehouse_ctx['branch_id']
                        vals['user_id'] = warehouse_ctx['user_id']
                        vals['invoice_journal_id'] = warehouse_ctx['journal_id']
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
                        currency = self._confirmed_order_currency(
                            each.get('currency'), currency_cache)
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
                            try:
                                new_lines = self.env['sale.order.line'].sudo().create(
                                    line_vals_list)
                            except Exception:
                                _logger.exception(
                                    'Shopify order import: could not create the '
                                    'lines of order %s (shopify id %s); rolling '
                                    'the order back.',
                                    each.get('name'), shopify_id)
                                # Re-raise so the order's savepoint rolls the
                                # whole order back. Swallowing it here would keep
                                # a line-less order that looks imported, and -- if
                                # the failure was a database error -- would leave
                                # the transaction aborted for every order after
                                # it, because the savepoint would never roll back.
                                raise
                        else:
                            new_lines = self.env['sale.order.line'].browse()
                        # if not wizard.draft:
                        #     so.action_confirm()
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
                # only counted once the order is committed to its savepoint
                existing_refs.add(str(shopify_id))
                imported += 1
            except Exception:
                # one bad order must not abort the page, but it must leave a
                # trace -- the old bare `continue` hid every failure, so an
                # order that never imported looked exactly like one that had
                # nothing to do
                failed += 1
                _logger.exception(
                    'Shopify order import failed for order %s (shopify id '
                    '%s) on instance %s; skipping it.',
                    each.get('name'), each.get('id'), instance.display_name)
                # The savepoint has rolled this order back, which also cleared
                # any aborted-transaction state, so a log row can still be
                # written here -- that was impossible before, when a database
                # error left the cursor unusable for the rest of the page.
                try:
                    self.env['log.message'].sudo().create([{
                        'name': 'Order %s (shopify id %s) failed to import; '
                                'see the server log for the traceback.' % (
                                    each.get('name'), each.get('id')),
                        'shopify_instance_id': instance.id,
                        'model': 'sale.order',
                    }])
                except Exception:
                    _logger.exception(
                        'Shopify order import: could not record the failure '
                        'of order %s in log.message.', each.get('id'))
                continue

        _logger.info(
            'Shopify order import: %s order(s) in the page, %s imported, '
            '%s failed, %s already present or skipped.',
            len(shopify_orders), imported, failed,
            len(shopify_orders) - imported - failed)

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
                        [('name', '=', '14%'),
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

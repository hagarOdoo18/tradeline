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
from odoo import api, models, fields, _
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class SyncCustomer(models.TransientModel):
    """ Class for the transient model sync. customer
        Methods:
            sync_customers(self):
                Method to create queue jobs for exporting and importing data.
            export_partners_to_shopify(self,partner):
                method to export partners from odoo to shopify.Queue job
                evokes this method to export odoo partners.
            import_customers_from_shopify(self,shopify_customers):
                method to import partners from shopify to odoo.Queue job
                evokes this method for creating partners in odoo.
    """
    _name = 'sync.customer'
    _description = 'Sync Customer'

    import_customers = fields.Selection(string='Import/Export',
                                        selection=[('shopify', 'To Shopify'),
                                                   ('odoo', 'From Shopify')],
                                        required=True, default='odoo',
                                        help='Selection field for choose data'
                                             ' exchange type.')
    shopify_instance_id = fields.Many2one('shopify.configuration',
                                          string="Shopify Instance",
                                          required=True,
                                          help='Id of shopify instance')

    @api.model
    def _cron_import_customers_from_shopify(self):
        """Scheduled action to import customers from Shopify for all active
        connected instances. For each instance a transient sync.customer
        record is created in the 'From Shopify' direction and sync_customers
        is called, which fetches the customers (paginated) and queues
        import_customers_from_shopify job.cron records that _do_job then
        processes."""
        instances =  self.env['shopify.configuration'].search(
                    [('company_id', '=', self.env.company.id)])
        for instance in instances:
            try:
                wizard = self.sudo().create({
                    'import_customers': 'odoo',
                    'shopify_instance_id': instance.id,
                })
                wizard.sync_customers()
            except Exception as error:
                _logger.error(
                    'Failed to queue customer import for Shopify instance '
                    '%s: %s', instance.name, str(error))

    def sync_customers(self):
        """Method to create queue jobs for exporting and importing data."""
        model = self.env['ir.model'].search([('model', '=', "sync.customer")])
        shopify_instance = self.shopify_instance_id
        store_name = self.shopify_instance_id.shop_name
        version = self.shopify_instance_id.version
        if (self.import_customers == 'shopify' and
                not self.shopify_instance_id.export_customer):
            raise ValidationError(_('For Syncing Customers to Shopify Enable '
                                    'Export Customers option in shopify '
                                    'configuration '))
        else:
            if self.import_customers == 'shopify':
                partners = self.env['res.partner'].search(
                    [('company_id', 'in',
                      [False, shopify_instance.company_id.id]),
                     ('type', '=', 'contact')])
                partner_list = []
                partner_id_list = []
                size = 50
                for i in range(0, len(partners), size):
                    partner_list.append(partners[i:i + size])
                for partner in partner_list:
                    for item in partner:
                        if (self.shopify_instance_id.id not in
                                item.shopify_sync_ids.ids):
                            partner_id_list.append(item.id)
                    self.env['job.cron'].sudo().create(
                        [{
                            'model_id': model.id,
                            'function': "export_partners_to_shopify",
                            'data': partner_id_list,
                            'instance_id': self.shopify_instance_id.id,
                        }])
                    partner_id_list = []
            else:
                customer_url = ('https://%s/admin/api/%s/customers.json'
                                % (store_name, version))
                headers = shopify_instance._get_shopify_headers()
                response = requests.request('GET', customer_url,verify=False,
                                            headers=headers, data=[])
                if 'customers' in response.json():
                    shopify_customers = response.json()['customers']
                    self.env['job.cron'].sudo().create([{
                        'model_id': model.id,
                        'function': "import_customers_from_shopify",
                        'data': shopify_customers,
                        'instance_id': self.shopify_instance_id.id,
                    }])
                    _logger.info('++++++++++customers++++++++++++++++++++')
                customer_link = response.headers[
                    'link'] if 'link' in response.headers else ''
                customer_links = customer_link.split(',')
                for link in customer_links:
                    match = re.compile(r'rel=\"next\"').search(link)
                    if match:
                        customer_link = link
                rel = re.search('rel=\"(.*)\"', customer_link).group(
                    1) if 'link' in response.headers else ''
                if customer_link and rel == 'next':
                    i = 0
                    n = 1
                    while i < n:
                        page_info = re.search('page_info=(.*)>',
                                              customer_link).group(1)
                        limit = re.search('limit=(.*)&',
                                          customer_link).group(1)
                        customer_link = (('https://%s/admin/api/%s/'
                                          'customers.json?limit=%s'
                                          '&page_info=%s')
                                         % (store_name, version, limit,
                                            page_info))
                        response = requests.request('GET', customer_link,
                                                    headers=headers, data=[])
                        if 'customers' in response.json():
                            customers = response.json()['customers']
                            self.env['job.cron'].sudo().create([{
                                'model_id': model.id,
                                'function': "import_customers_from_shopify",
                                'data': customers,
                                'instance_id': self.shopify_instance_id.id,
                            }])
                        customer_link = response.headers['link']
                        customer_links = customer_link.split(',')
                        for link in customer_links:
                            match = re.compile(r'rel=\"next\"').search(link)
                            if match:
                                customer_link = link
                        rel = re.search('rel=\"next\"', customer_link)
                        i += 1
                        if customer_link and rel is not None:
                            n += 1

    def export_partners_to_shopify(self, lists, instance):
        """Method to export partners from odoo to shopify.
            Queue job evokes this method to export odoo partners.
            partner(list):list of dictionary with odoo partner details.
        """
        store_name = instance.shop_name
        version = instance.version
        customer_url = 'https://%s/admin/api/%s/customers.json' % (
            store_name, version)
        headers = instance._get_shopify_headers()
        partner = self.env['res.partner'].sudo().search([('id', 'in', lists)])
        for customer in partner:
            instance_ids = customer.shopify_sync_ids.mapped('instance_id.id')
            if instance.id not in instance_ids:
                payload = json.dumps({
                    'customer': {
                        'first_name': customer.name,
                        'last_name': '',
                        'email': customer.email or '',
                        'verified_email': True,
                        'addresses': [
                            {
                                'address1': customer.street,
                                'city': customer.city,
                                'province': customer.state_id.name or '',
                                'zip': customer.zip,
                                'last_name': '',
                                'first_name': customer.name,
                                'country': customer.country_id.name or ''
                            }
                        ],
                        'send_email_invite': True
                    }
                })
                response = requests.request('POST', customer_url,
                                            headers=headers, data=payload)
                if response.status_code == 201:
                    response_rec = response.json()
                    response_customer_id = response_rec['customer']['id']
                    customer.shopify_sync_ids.sudo().create({
                        'instance_id': instance.id,
                        'shopify_customer_ref': response_customer_id,
                        'customer_id': customer.id,
                    })

    @staticmethod
    def _shopify_customer_phones(customer):
        """Return the phone spellings a Shopify customer may match on.

        A number stored in Odoo without its '+2' Egyptian prefix must still
        resolve to the same partner, so both spellings are candidates.
        """
        phone = (customer.get('phone') or '').strip()
        if not phone:
            return []
        if phone.startswith('+2'):
            return [phone, phone[2:]]
        return [phone]

    def _prefetch_import_customers(self, shopify_customers):
        """Resolve, in one query each, everything the import loop looks up
        per customer: partners by mobile, countries and states by name, and
        the Shopify ids that already carry a shopify.sync line.

        Doing this per customer is what made the import slow: five searches
        for every record in the page, on unindexed columns and on a
        translated (jsonb) country name.
        """
        phones = set()
        country_names = set()
        state_names = set()
        for customer in shopify_customers:
            phones.update(self._shopify_customer_phones(customer))
            addresses = customer.get('addresses') or []
            if addresses:
                if addresses[0].get('country'):
                    country_names.add(addresses[0]['country'])
                if addresses[0].get('province'):
                    state_names.add(addresses[0]['province'])

        partners_by_mobile = {}
        if phones:
            # ordered by id so a duplicated mobile always resolves to the
            # same, oldest partner instead of an arbitrary one
            for partner in self.env['res.partner'].sudo().search(
                    [('mobile', 'in', list(phones))], order='id asc'):
                partners_by_mobile.setdefault(partner.mobile, partner)

        countries_by_name = {}
        if country_names:
            for country in self.env['res.country'].sudo().search(
                    [('name', 'in', list(country_names))]):
                countries_by_name.setdefault(country.name, country.id)

        states_by_name = {}
        if state_names:
            for state in self.env['res.country.state'].sudo().search(
                    [('name', 'in', list(state_names))], order='id asc'):
                states_by_name.setdefault(state.name, state.id)

        shopify_ids = [str(customer['id']) for customer in shopify_customers
                       if customer.get('id')]
        synced_refs = set()
        if shopify_ids:
            synced_refs = set(self.env['shopify.sync'].sudo().search(
                [('shopify_customer_ref', 'in', shopify_ids)]
            ).mapped('shopify_customer_ref'))

        return (partners_by_mobile, countries_by_name, states_by_name,
                synced_refs)

    @staticmethod
    def _shopify_customer_vals(customer, instance, countries_by_name,
                               states_by_name):
        """Build the res.partner values for one Shopify customer."""
        vals = {}
        addresses = customer.get('addresses') or []
        if addresses:
            address = addresses[0]
            vals.update({
                'street': address.get('address1'),
                'street2': address.get('address2'),
                'city': address.get('city'),
                'country_id': countries_by_name.get(address.get('country'),
                                                    False),
                'state_id': states_by_name.get(address.get('province'), False),
                'zip': address.get('zip'),
            })
        first_name = customer.get('first_name')
        last_name = customer.get('last_name')
        if first_name and last_name:
            vals['name'] = '%s %s' % (first_name, last_name)
        elif first_name:
            vals['name'] = first_name
        elif last_name:
            vals['name'] = last_name
        elif customer.get('email'):
            vals['name'] = customer['email']
        vals.update({
            'email': customer.get('email'),
            'mobile': customer.get('phone'),
            'shopify_customer_ref': customer.get('id'),
            'shopify_instance_id': instance.id,
            'synced_customer': True,
            'company_id': instance.company_id.id,
        })
        return vals

    def import_customers_from_shopify(self, shopify_customers, instance):
        """Method to import partners from shopify to odoo.
            Queue job evokes this method for creating partners in odoo.

            shopify_customers(list):list of dictionary with shopify partner
            details.

        Every partner write goes out under `shopify_no_export`. Without it
        res.partner.write() pushes each imported customer straight back to
        Shopify — a GET plus a PUT per record against a rate-limited API,
        which dominated the runtime of this job. An import must never echo
        the imported data back out.
        """
        if not shopify_customers:
            return
        shopify_instance = instance
        partner_model = self.env['res.partner'].sudo().with_context(
            shopify_no_export=True)
        (partners_by_mobile, countries_by_name, states_by_name,
         synced_refs) = self._prefetch_import_customers(shopify_customers)

        sync_vals = []
        log_vals = []
        for customer in shopify_customers:
            try:
                exist_customer = None
                for phone in self._shopify_customer_phones(customer):
                    exist_customer = partners_by_mobile.get(phone)
                    if exist_customer:
                        break
                vals = self._shopify_customer_vals(
                    customer, shopify_instance, countries_by_name,
                    states_by_name)
                shopify_ref = str(customer.get('id'))

                if not exist_customer:
                    if not customer.get('first_name'):
                        log_vals.append({
                            'name': 'Customer Creation not processed for '
                                    'shopify id : ' + shopify_ref,
                            'shopify_instance_id': shopify_instance.id,
                            'model': 'res.partner',
                        })
                        continue
                    # a savepoint per record: a database error on one
                    # customer rolls back that customer only, instead of
                    # poisoning the transaction for the rest of the page
                    with self.env.cr.savepoint():
                        new_customer = partner_model.create(vals)
                    # keep the page's own duplicates resolving to this new
                    # partner instead of creating it twice
                    for phone in self._shopify_customer_phones(customer):
                        partners_by_mobile.setdefault(phone, new_customer)
                    sync_vals.append({
                        'instance_id': instance.id,
                        'shopify_customer_ref': customer['id'],
                        'customer_id': new_customer.id,
                    })
                    synced_refs.add(shopify_ref)
                    log_vals.append({
                        'name': 'Customer Creation  processed for '
                                'shopify id : ' + shopify_ref,
                        'shopify_instance_id': shopify_instance.id,
                        'model': 'res.partner',
                    })
                else:
                    # one write, not three: the instance and the Shopify ref
                    # are already in vals
                    with self.env.cr.savepoint():
                        exist_customer.with_context(
                            shopify_no_export=True).write(vals)
                    if shopify_ref not in synced_refs:
                        sync_vals.append({
                            'instance_id': instance.id,
                            'shopify_customer_ref': customer['id'],
                            'customer_id': exist_customer.id,
                        })
                        synced_refs.add(shopify_ref)
            except Exception:
                # one bad record must not abort the page, but it must leave
                # a trace — the old bare `continue` hid every failure
                _logger.exception(
                    'Shopify customer import failed for shopify id %s '
                    '(instance %s); skipping this record.',
                    customer.get('id'), shopify_instance.display_name)
                continue

        if sync_vals:
            self.env['shopify.sync'].sudo().create(sync_vals)
        if log_vals:
            self.env['log.message'].sudo().create(log_vals)
        _logger.info(
            'Shopify customer import: %d record(s), %d sync line(s) created.',
            len(shopify_customers), len(sync_vals))

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
from odoo import models, fields, _
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
                response = requests.request('GET', customer_url,
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

    def import_customers_from_shopify(self, shopify_customers, instance):
        """Method to import partners from shopify to odoo.
            Queue job evokes this method for creating partners in odoo.

            shopify_customers(list):list of dictionary with shopify partner
            details.
        """
        shopify_instance = instance
        for customer in shopify_customers:
            exist_customers = self.env['res.partner'].search(
                [('shopify_customer_ref', '=', customer['id']),
                 ('shopify_instance_id', '=', shopify_instance.id)])
            if not exist_customers:
                vals = {}
                if customer['addresses']:
                    country_id = self.env['res.country'].sudo().search([
                        ('name', '=', customer['addresses'][0]['country'])
                    ])
                    state_id = self.env['res.country.state'].sudo().search([
                        ('name', '=', customer['addresses'][0]['province'])
                    ])
                    vals = {
                        'street': customer['addresses'][0]['address1'],
                        'street2': customer['addresses'][0]['address2'],
                        'city': customer['addresses'][0]['city'],
                        'country_id': country_id.id if country_id else False,
                        'state_id': state_id.id if state_id else False,
                        'zip': customer['addresses'][0]['zip'],
                    }
                if customer['first_name']:
                    vals['name'] = customer['first_name']
                if customer['last_name']:
                    if customer['first_name']:
                        vals['name'] = (customer['first_name'] + ' ' +
                                        customer['last_name'])
                    else:
                        vals['name'] = customer['last_name']
                if (not customer['first_name'] and
                        not customer['last_name'] and customer['email']):
                    vals['name'] = customer['email']
                vals['email'] = customer['email']
                vals['phone'] = customer['phone']
                vals['shopify_customer_ref'] = customer['id']
                vals['shopify_instance_id'] = shopify_instance.id
                vals['synced_customer'] = True
                vals['company_id'] = shopify_instance.company_id.id
                if customer['first_name']:
                    new_customer = self.env['res.partner'].sudo().create(vals)
                    new_customer.shopify_sync_ids.sudo().create({
                        'instance_id': instance.id,
                        'shopify_customer_ref': customer['id'],
                        'customer_id': new_customer.id,
                    })
                else:
                    self.env['log.message'].sudo().create([{
                        'name': 'Customer Creation not processed for '
                                'shopify id : ' + str(customer['id']),
                        'shopify_instance_id': self.shopify_instance_id.id,
                        'model': 'res.partner',
                    }])
            else:
                vals = {}
                if customer['addresses']:
                    country_id = self.env['res.country'].sudo().search([
                        ('name', '=', customer['addresses'][0]['country'])
                    ])
                    state_id = self.env['res.country.state'].sudo().search([
                        ('name', '=', customer['addresses'][0]['province'])
                    ])
                    vals = {
                        'street': customer['addresses'][0]['address1'],
                        'street2': customer['addresses'][0]['address2'],
                        'city': customer['addresses'][0]['city'],
                        'country_id': country_id.id if country_id else False,
                        'state_id': state_id.id if state_id else False,
                        'zip': customer['addresses'][0]['zip'],
                    }
                if customer['first_name']:
                    vals['name'] = customer['first_name']
                if customer['last_name']:
                    if customer['first_name']:
                        vals['name'] = (customer['first_name'] + ' ' +
                                        customer['last_name'])
                if (not customer['first_name'] and
                        not customer['last_name'] and customer['email']):
                    vals['name'] = customer['email']
                vals['email'] = customer['email']
                vals['phone'] = customer['phone']
                vals['shopify_customer_ref'] = customer['id']
                vals['shopify_instance_id'] = shopify_instance.id
                vals['synced_customer'] = True
                vals['company_id'] = shopify_instance.company_id.id
                self.env['res.partner'].sudo().write(vals)

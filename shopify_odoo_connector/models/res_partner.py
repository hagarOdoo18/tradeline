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
import requests
from odoo import fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Partner fields that are mirrored to Shopify. Only writes touching one of
# these trigger a sync request.
SHOPIFY_SYNCED_FIELDS = (
    'name', 'email', 'phone', 'street', 'city', 'country_id', 'zip',
)


class ResPartners(models.Model):
    """Class for inherited model res. partner
        Methods:
            sync_shopify_customer(self):
                Method to sync odoo partners into shopify.
    """
    _inherit = 'res.partner'

    shopify_instance_id = fields.Many2one('shopify.configuration',
                                          string="Shopify Instance",
                                          help='Shopify instance id of partner')
    synced_customer = fields.Boolean(readonly=True, string='Synced Product',
                                     help='Will be true for synced customer.')
    shopify_customer_ref = fields.Char(string='Shopify Id', readonly=True,
                                       help='Partner id in shopify')
    shopify_sync_ids = fields.One2many('shopify.sync',
                                       'customer_id',
                                       string='Shopify Sync',
                                       help='shopify sync ids')

    def sync_shopify_customer(self):
        """Method to sync odoo partners into shopify."""
        if not self.email:
            raise UserError(_('Provide a valid email id'))
        instance = self.shopify_instance_id
        store_name = instance.shop_name
        version = instance.version
        customer_url = 'https://%s/admin/api/%s/customers.json' % (
            store_name, version)
        instance_ids = self.shopify_sync_ids.mapped('instance_id.id')
        if instance.id not in instance_ids:
            payload = json.dumps({
                'customer': {
                    'first_name': self.name,
                    'last_name': '',
                    'email': self.email or '',
                    'verified_email': True,
                    'addresses': [
                        {
                            'address1': self.street,
                            'city': self.city,
                            'province': self.state_id.name or '',
                            'zip': self.zip,
                            'last_name': '',
                            'first_name': self.name,
                            'country': self.country_id.name or ''
                        }
                    ],
                    'send_email_invite': True
                }
            })
            response = requests.request('POST', customer_url,
                                        headers=instance._get_shopify_headers(),
                                        data=payload)
            response_rec = response.json()
            if response_rec.get('customer'):
                response_customer_id = response_rec['customer']['id']
                self.shopify_sync_ids.sudo().create({
                    'instance_id': instance.id,
                    'shopify_customer_ref': response_customer_id,
                    'customer_id': self.id,
                })

    def write(self, vals):
        """Write the partner, then push the change to Shopify.

        The Odoo write is always performed first and unconditionally: the
        Shopify sync is a side effect and must never be able to skip or
        block saving the record.
        """
        res = super().write(vals)
        if any(field in vals for field in SHOPIFY_SYNCED_FIELDS):
            self._sync_shopify_customer_updates(vals)
        return res

    def _sync_shopify_customer_updates(self, vals):
        """Push updated partner data to every Shopify instance it is
        synced with. Failures are logged and never block the Odoo write."""
        configs = self.env['shopify.configuration'].sudo().search(
            [('company_id', '=', self.env.company.id)])
        if not configs:
            return
        for config in configs:
            for partner in self:
                for rec in partner.shopify_sync_ids:
                    if not rec.customer_id or not rec.shopify_customer_ref:
                        continue
                    if rec.instance_id and rec.instance_id != config:
                        continue
                    try:
                        partner._push_shopify_customer(config, rec, vals)
                    except Exception:
                        _logger.exception(
                            'Shopify sync failed for partner %s (id %s) on '
                            'instance %s; the Odoo record was saved anyway.',
                            partner.display_name, partner.id, config.display_name)

    def _push_shopify_customer(self, config, rec, vals):
        """Send one customer update to Shopify."""
        self.ensure_one()
        store_name = config.shop_name
        version = config.version
        partner_url = ('https://%s/admin/api/%s/customers/'
                       '%s.json') % (
                          store_name, version, rec.shopify_customer_ref)
        address_url = ('https://%s/admin/api/%s/customers/'
                       '%s/addresses.json') % (
                          store_name, version, rec.shopify_customer_ref)
        headers = config._get_shopify_headers()
        partner = requests.request('GET', address_url,
                                   headers=headers, timeout=30)
        line_vals = {'id': rec.shopify_customer_ref}

        addresses = partner.json().get('addresses', [])
        address = {'id': addresses[0]['id']} if addresses else {}
        if 'name' in vals.keys():
            line_vals['first_name'] = vals['name']
            address['first_name'] = vals['name']
        if 'email' in vals.keys():
            line_vals['email'] = vals['email']
        if 'phone' in vals.keys():
            line_vals['phone'] = ''.join(
                c for c in vals['phone'] or '' if c.isdigit())
            address['phone'] = ''.join(
                c for c in vals['phone'] or '' if c.isdigit())
        if 'street' in vals.keys():
            address['address1'] = vals['street']
        if 'city' in vals.keys():
            address['city'] = vals['city']
        if 'country_id' in vals.keys():
            address['country'] = self.env['res.country'].browse(
                [vals['country_id']]).name
        if 'zip' in vals.keys():
            address['zip'] = vals['zip']
        if any(key != 'id' for key in line_vals):
            line_vals['addresses'] = [address]
            payload = json.dumps({'customer': line_vals})
            requests.request('PUT', partner_url,
                             headers=headers, data=payload, timeout=30)

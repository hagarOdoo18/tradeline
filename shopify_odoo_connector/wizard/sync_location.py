# -*- coding: utf-8 -*-
import json
import requests
from odoo import fields, models, _
from odoo.exceptions import ValidationError


class SyncLocation(models.TransientModel):
    """Wizard to sync locations between Shopify and Odoo."""
    _name        = 'sync.location'
    _description = 'Sync Shopify Locations'

    shopify_instance_id = fields.Many2one(
        'shopify.configuration',
        string='Shopify Instance',
        required=True,
    )
    warehouse_ids = fields.Many2many(
        'stock.warehouse',
        string='Warehouses to Push',
        help='Select Odoo warehouses to create as locations in Shopify.',
    )

    # ------------------------------------------------------------------
    # Pull: Shopify → Odoo
    # ------------------------------------------------------------------

    def action_sync_locations(self):
        """Fetch all Shopify locations and upsert shopify.location records."""
        self.ensure_one()
        instance   = self.shopify_instance_id
        store_name = instance.shop_name
        version    = instance.version
        headers    = instance._get_shopify_headers()

        url      = "https://%s/admin/api/%s/locations.json" % (store_name, version)
        response = requests.get(url, headers=headers)

        if response.status_code != 200:
            raise ValidationError(_(
                'Failed to fetch locations from Shopify: %s' % response.text
            ))

        locations = response.json().get('locations', [])
        if not locations:
            raise ValidationError(
                _('No locations found in this Shopify instance.'))

        Location = self.env['shopify.location'].sudo()

        for loc in locations:
            address = ', '.join(filter(None, [
                loc.get('address1', ''),
                loc.get('city', ''),
                loc.get('province', ''),
                loc.get('country', ''),
            ]))
            existing = Location.search([
                ('shopify_location_id', '=', str(loc['id'])),
                ('instance_id',         '=', instance.id),
            ], limit=1)
            vals = {
                'name':                loc.get('name', ''),
                'shopify_location_id': str(loc['id']),
                'instance_id':         instance.id,
                'address':             address,
                'active':              loc.get('active', True),
            }
            if existing:
                existing.write(vals)
            else:
                Location.create(vals)

        return {
            'type':      'ir.actions.act_window',
            'name':      'Shopify Locations',
            'res_model': 'shopify.location',
            'view_mode': 'list,form',
            'domain':    [('instance_id', '=', instance.id)],
            'target':    'current',
        }

    # ------------------------------------------------------------------
    # Push: Odoo → Shopify
    # ------------------------------------------------------------------

    def action_push_locations(self):
        """Create selected Odoo warehouses as locations in Shopify."""
        self.ensure_one()
        if not self.warehouse_ids:
            raise ValidationError(
                _('Please select at least one warehouse to push.'))

        instance   = self.shopify_instance_id
        store_name = instance.shop_name
        version    = instance.version
        headers    = instance._get_shopify_headers()
        Location   = self.env['shopify.location'].sudo()

        url = "https://%s/admin/api/%s/locations.json" % (store_name, version)

        for warehouse in self.warehouse_ids:
            # skip warehouses already pushed to this instance
            already = Location.search([
                ('warehouse_id', '=', warehouse.id),
                ('instance_id',  '=', instance.id),
            ], limit=1)
            if already:
                continue

            partner = warehouse.partner_id
            payload = json.dumps({
                'location': {
                    'name':         warehouse.name,
                    'address1':     partner.street  or '',
                    'city':         partner.city    or '',
                    'zip':          partner.zip     or '',
                    'country_code': partner.country_id.code if partner.country_id else '',
                    'phone':        partner.phone   or '',
                }
            })

            response = requests.post(url, headers=headers, data=payload)

            if response.status_code in (200, 201):
                loc = response.json().get('location', {})
                address = ', '.join(filter(None, [
                    loc.get('address1', ''),
                    loc.get('city', ''),
                    loc.get('province', ''),
                    loc.get('country', ''),
                ]))
                Location.create({
                    'name':                loc.get('name', warehouse.name),
                    'shopify_location_id': str(loc['id']),
                    'instance_id':         instance.id,
                    'warehouse_id':        warehouse.id,
                    'address':             address,
                    'active':              loc.get('active', True),
                })
            else:
                self.env['log.message'].sudo().create([{
                    'name': (
                        'Failed to push warehouse "%s" to Shopify: %s'
                        % (warehouse.name, response.text)
                    ),
                    'shopify_instance_id': instance.id,
                    'model': 'Stock Warehouse',
                }])

        return {
            'type':      'ir.actions.act_window',
            'name':      'Shopify Locations',
            'res_model': 'shopify.location',
            'view_mode': 'list,form',
            'domain':    [('instance_id', '=', instance.id)],
            'target':    'current',
        }

# -*- coding: utf-8 -*-
from odoo import fields, models


class ShopifyLocation(models.Model):
    """Stores the mapping between a Shopify location and an Odoo warehouse."""
    _name        = 'shopify.location'
    _description = 'Shopify Location'
    _rec_name    = 'name'

    name               = fields.Char(string='Shopify Location', readonly=True)
    shopify_location_id = fields.Char(string='Shopify Location ID', readonly=True)
    instance_id        = fields.Many2one('shopify.configuration',
                                         string='Instance', readonly=True,
                                         ondelete='cascade')
    warehouse_id       = fields.Many2one('stock.warehouse',
                                         string='Odoo Warehouse',
                                         help='Map this Shopify location to '
                                              'an Odoo warehouse')
    address            = fields.Char(string='Address', readonly=True)
    active             = fields.Boolean(string='Active in Shopify',
                                        default=True, readonly=True)

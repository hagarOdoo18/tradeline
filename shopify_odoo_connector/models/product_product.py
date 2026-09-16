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
from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

SHOPIFY_TIMEOUT = 60


class ProductProduct(models.Model):
    """Class for inherited model product.product.

        Methods:
            sync_shopify_product(self):
                Push this variant's price and sellable quantity to Shopify.
    """
    _inherit = 'product.product'

    shopify_variant_sku = fields.Char(readonly=False, index=True,
                                  string='Shopify SKU',
                                  help='Shopify SKU of product variant')

    shopify_variant = fields.Char(readonly=True,
                                  string='Shopify Variant',
                                  help='Shopify id of product variant')
    shopify_instance_id = fields.Many2one('shopify.configuration',
                                          string='Shopify Instance',
                                          help='Shopify Instance id of product '
                                               'variant')
    shopify_sync_ids = fields.One2many('shopify.sync',
                                       'product_prod_id',
                                       string='Shopify Sync',
                                       help='Shopify sync ids')

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _shopify_syncs_by_instance(self):
        """Return {shopify.configuration: shopify.sync recordset} for these
        variants, covering only variants already linked to a Shopify variant.
        """
        syncs = self.env['shopify.sync'].sudo().search([
            ('product_prod_id', 'in', self.ids),
            ('shopify_variant_id', '!=', False),
            ('instance_id', '!=', False),
        ])
        grouped = {}
        for sync in syncs:
            grouped.setdefault(
                sync.instance_id, self.env['shopify.sync'].sudo().browse())
            grouped[sync.instance_id] |= sync
        return grouped

    def _shopify_graphql(self, instance, query, variables):
        """Post a GraphQL query to Shopify and return the decoded response."""
        url = 'https://%s/admin/api/%s/graphql.json' % (
            instance.shop_name, instance.version)
        response = requests.post(
            url,
            headers=instance._get_shopify_headers(),
            data=json.dumps({'query': query, 'variables': variables}),
            timeout=SHOPIFY_TIMEOUT,
        )
        if response.status_code != 200:
            raise UserError(_(
                'Shopify returned HTTP %(code)s: %(body)s',
                code=response.status_code,
                body=(response.text or '')[:300]))
        return response.json()

    def _shopify_log(self, instance, message):
        self.env['log.message'].sudo().create([{
            'name': message,
            'shopify_instance_id': instance.id,
            'model': 'product.product',
        }])

    # ------------------------------------------------------------------
    # price
    # ------------------------------------------------------------------

    def _push_shopify_price(self, instance, syncs):
        """Update the Shopify price of the given variants.

        The price sent is the variant's own sales price (`lst_price`), which
        already carries the attribute `price_extra`. REST variant endpoints
        are deprecated from API version 2025-04, so this goes over GraphQL -
        the same `productVariantsBulkUpdate` mutation the pricing wizard uses.
        The mutation takes one product at a time, so the variants are grouped
        by their Shopify product id: one call per product, not per variant.

        Returns (pushed, problems). It never raises on a Shopify rejection:
        raising would roll the transaction back and take the log.message rows
        - the only durable record of what happened - with it.
        """
        # imported here, not at module level, so the addon's load order can
        # never matter: by the time a button runs, everything is imported.
        from odoo.addons.shopify_odoo_connector.wizard.sync_pricing import (
            PRODUCT_VARIANTS_BULK_UPDATE)

        problems = []
        by_product = {}
        for sync in syncs:
            product = sync.product_prod_id
            if not product:
                continue
            # The Shopify PRODUCT id lives on the template. shopify.sync rows
            # for a variant carry the VARIANT id in their own shopify_product
            # column (see sync_product.py), so reading it here would address
            # the mutation at a variant id and Shopify would reject the call.
            shopify_product_id = product.product_tmpl_id.shopify_product
            if not shopify_product_id:
                problems.append(
                    'variant %s (%s): its product has no Shopify product id'
                    % (sync.shopify_variant_id, product.display_name))
                continue
            by_product.setdefault(shopify_product_id, {})[
                str(sync.shopify_variant_id)] = product

        pushed = 0
        for shopify_product_id, variant_products in by_product.items():
            # keyed by variant id above: a re-imported variant can have two
            # shopify.sync rows, and repeating an id in the payload makes
            # Shopify reject the whole mutation
            variants = [{
                'id': 'gid://shopify/ProductVariant/%s' % variant_id,
                'price': '%.2f' % (product.lst_price or 0.0),
            } for variant_id, product in variant_products.items()]

            try:
                result = self._shopify_graphql(
                    instance, PRODUCT_VARIANTS_BULK_UPDATE, {
                        'productId': ('gid://shopify/Product/%s'
                                      % shopify_product_id),
                        'variants': variants,
                    })
            except (UserError, requests.exceptions.RequestException) as error:
                # collected, not raised: a raise here would roll back the
                # log rows written for the products already pushed
                problems.append('product %s: %s' % (shopify_product_id, error))
                continue

            # top level errors (bad query, bad id, missing write_products scope)
            if result.get('errors'):
                problems.append('product %s: %s' % (
                    shopify_product_id, json.dumps(result['errors'])))
                continue
            data = (result.get('data') or {}).get(
                'productVariantsBulkUpdate') or {}
            user_errors = data.get('userErrors') or []
            if user_errors:
                problems.append('product %s: %s' % (
                    shopify_product_id, json.dumps(user_errors)))
                continue

            pushed += len(variants)
            self._shopify_log(instance, 'Price push done for product %s: %s' % (
                shopify_product_id,
                ', '.join('%s=%s' % (variant['id'].split('/')[-1],
                                     variant['price'])
                          for variant in variants)))

        if problems:
            self._shopify_log(
                instance, 'Price push failed:\n' + '\n'.join(problems))
        return pushed, problems

    # ------------------------------------------------------------------
    # quantity
    # ------------------------------------------------------------------

    def _push_shopify_quantity(self, instance, products):
        """Update the Shopify inventory level of the given variants.

        This delegates to `sync.inventory` rather than posting on its own, so
        a manual push produces exactly the number the scheduled push would:
        the same barcode / shopify_variant_sku grouping, the same "keep 3 in
        reserve" rule, the same retry and logging. Anything else and the
        button and the cron would keep overwriting each other.
        """
        sync_inventory = self.env['sync.inventory'].sudo()
        # same filter _sync_to_shopify applies, so the friendly message here
        # is not followed by a raw ValidationError from inside the wizard
        locations = self.env['shopify.location'].sudo().search([
            ('instance_id', '=', instance.id),
            ('warehouse_id', '!=', False),
            ('active', '=', True),
        ]).filtered(lambda loc: loc.shopify_location_id)
        if not locations:
            return 0, [_('No warehouse is mapped to a Shopify location for '
                         'instance %s. Map one with the Sync Locations wizard '
                         'first.') % instance.name]

        groups = sync_inventory._build_inventory_groups(
            instance, products=products)
        if not groups:
            return 0, []

        # resolve the inventory item ids up front, the way the cron does, so
        # the push itself makes no extra call
        item_map = sync_inventory._get_inventory_item_map(
            instance,
            {variant_id for group in groups
             for variant_id in group['variant_ids']})
        for group in groups:
            group['items'] = {variant_id: item_map[variant_id]
                              for variant_id in group['variant_ids']
                              if variant_id in item_map}

        unknown = [variant_id for group in groups
                   for variant_id in group['variant_ids']
                   if variant_id not in item_map]

        wizard = sync_inventory.create({
            'import_inventory': 'shopify',
            'shopify_instance_id': instance.id,
            'warehouse_ids': [(6, 0, locations.mapped('warehouse_id').ids)],
        })
        result = wizard._sync_to_shopify(groups=groups) or {}

        problems = []
        if unknown:
            problems.append(_('no Shopify inventory item found for variant(s) '
                              '%s') % ', '.join(unknown))
        if result.get('failed'):
            problems.append(_('%s inventory update(s) were rejected by '
                              'Shopify - see the Shopify logs')
                            % result['failed'])
        return result.get('pushed', 0), problems

    # ------------------------------------------------------------------
    # main action
    # ------------------------------------------------------------------

    def sync_shopify_product(self):
        """Push this variant's price and sellable quantity to Shopify.

        This updates a variant that is already linked to Shopify; it does not
        create anything. Creating the product on Shopify is the job of the
        Sync button on the product template.

        The quantity sent is the group total: the variants that share this
        one's barcode / shopify_variant_sku are summed together, exactly as
        the scheduled push does.
        """
        grouped = self._shopify_syncs_by_instance()
        if not grouped:
            raise UserError(_(
                'This variant is not linked to a Shopify variant yet. Export '
                'the product first with the Sync button on the product form.'))

        prices, quantities, problems = 0, 0, []
        for instance, syncs in grouped.items():
            # quantity first: it is the reversible half. A price already
            # accepted by Shopify cannot be rolled back with the transaction.
            pushed, issues = self._push_shopify_quantity(
                instance, syncs.mapped('product_prod_id'))
            quantities += pushed
            problems += issues

            pushed, issues = self._push_shopify_price(instance, syncs)
            prices += pushed
            problems += issues

        message = _('%(prices)s price(s) and %(quantities)s inventory level(s) '
                    'updated on Shopify.',
                    prices=prices, quantities=quantities)
        if problems:
            message += '\n' + '\n'.join(problems[:10])
        _logger.info('Shopify manual variant push: %s', message)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Shopify'),
                'message': message,
                'type': 'warning' if problems else 'success',
                'sticky': bool(problems),
            },
        }

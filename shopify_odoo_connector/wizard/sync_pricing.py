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
import time

import requests

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

# GraphQL mutation used to update the price of one or more variants of a
# single product in a single call. REST variant endpoints are deprecated from
# API version 2025-04 onwards, so pricing is pushed over GraphQL.
PRODUCT_VARIANTS_BULK_UPDATE = """
mutation productVariantsBulkUpdate($productId: ID!,
                                   $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    productVariants {
      id
      price
    }
    userErrors {
      field
      message
    }
  }
}
"""


class SyncPricing(models.TransientModel):
    """Wizard to push Odoo pricelist prices to Shopify variant prices.

        Methods:
            action_sync_pricing(self):
                Queue the selected products as job.cron batches.
            export_pricing_to_shopify(self, data, instance):
                Process a single queued batch (called by job.cron._do_job).
    """
    _name = 'sync.pricing'
    _description = 'Sync Pricing'

    shopify_instance_id = fields.Many2one(
        'shopify.configuration',
        string='Shopify Instance',
        required=True,
        help='Shopify instance the prices are pushed to',
    )
    pricelist_id = fields.Many2one(
        'product.pricelist',
        string='Pricelist',
        required=True,
        help='Odoo pricelist used to compute the price sent to Shopify',
    )
    product_ids = fields.Many2many(
        'product.template',
        string='Products',
        domain="[('shopify_product', '!=', False)]",
        help='Limit the push to these products. Leave empty to push every '
             'product already synced with the selected instance.',
    )
    batch_size = fields.Integer(
        string='Batch Size',
        default=20,
        required=True,
        help='Number of products handled by one queued job. Smaller batches '
             'are slower but safer on large catalogues.',
    )
    currency_id = fields.Many2one(
        'res.currency',
        string='Pricelist Currency',
        related='pricelist_id.currency_id',
        readonly=True,
        help='Currency of the selected pricelist',
    )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _get_synced_templates(self):
        """Return the product templates eligible for a price push."""
        domain = [
            ('shopify_product', '!=', False),
            ('shopify_instance_id', '=', self.shopify_instance_id.id),
        ]
        if self.product_ids:
            domain.append(('id', 'in', self.product_ids.ids))
        return self.env['product.template'].sudo().search(domain)

    def _get_variant_price(self, variant):
        """Compute the price of one variant from the selected pricelist.

        Falls back to the variant sales price when the pricelist cannot
        produce a price for the product."""
        try:
            price = self.pricelist_id._get_product_price(variant, 1.0)
        except Exception as error:
            _logger.warning(
                'Pricelist %s could not price variant %s (%s) - falling back '
                'to lst_price: %s',
                self.pricelist_id.display_name, variant.id,
                variant.display_name, error)
            price = variant.lst_price
        return float(self.pricelist_id.currency_id.round(price or 0.0))

    def _shopify_graphql(self, query, variables):
        """Post a GraphQL query to Shopify and return the decoded response."""
        instance = self.shopify_instance_id
        url = 'https://%s/admin/api/%s/graphql.json' % (
            instance.shop_name, instance.version)
        response = requests.post(
            url,
            headers=instance._get_shopify_headers(),
            data=json.dumps({'query': query, 'variables': variables}),
            timeout=60,
        )
        if response.status_code != 200:
            raise ValidationError(_(
                'Shopify returned HTTP %(code)s for the price update: '
                '%(body)s',
                code=response.status_code, body=response.text))
        return response.json()

    def _log(self, message):
        """Write a log.message record for the current instance."""
        self.env['log.message'].sudo().create([{
            'name': message,
            'shopify_instance_id': self.shopify_instance_id.id,
            'model': 'product.pricelist',
        }])

    @staticmethod
    def _throttle(result):
        """Sleep when the Shopify GraphQL leaky bucket is nearly empty."""
        throttle = (result.get('extensions', {})
                    .get('cost', {})
                    .get('throttleStatus', {}))
        available = throttle.get('currentlyAvailable')
        restore_rate = throttle.get('restoreRate') or 50
        if available is not None and available < 100:
            time.sleep(min(5.0, (100 - available) / float(restore_rate)))

    # ------------------------------------------------------------------
    # push
    # ------------------------------------------------------------------

    def _push_template_prices(self, template):
        """Push the prices of every synced variant of one template."""
        variants = template.product_variant_ids.filtered(
            lambda v: v.shopify_variant)
        if not variants:
            _logger.info(
                'Shopify pricing: product "%s" has no variant linked to a '
                'Shopify variant - skipped.', template.display_name)
            return

        payload_variants = [{
            'id': 'gid://shopify/ProductVariant/%s' % variant.shopify_variant,
            'price': '%.2f' % self._get_variant_price(variant),
        } for variant in variants]

        result = self._shopify_graphql(PRODUCT_VARIANTS_BULK_UPDATE, {
            'productId': 'gid://shopify/Product/%s' % template.shopify_product,
            'variants': payload_variants,
        })

        # Top level GraphQL errors (bad query, bad id, missing scope, ...)
        if result.get('errors'):
            self._log('Price push failed for product %s (%s): %s' % (
                template.display_name, template.shopify_product,
                json.dumps(result['errors'])))
            return

        data = (result.get('data') or {}).get(
            'productVariantsBulkUpdate') or {}
        user_errors = data.get('userErrors') or []
        if user_errors:
            self._log('Price push rejected for product %s (%s): %s' % (
                template.display_name, template.shopify_product,
                json.dumps(user_errors)))
            return

        self._log('Price push done for product %s (%s): %s variant(s) - %s' % (
            template.display_name, template.shopify_product,
            len(payload_variants),
            ', '.join('%s=%s' % (v['id'].split('/')[-1], v['price'])
                      for v in payload_variants)))
        self._throttle(result)

    def _sync_to_shopify(self, templates):
        """Push prices for the given templates, one GraphQL call each."""
        for template in templates:
            try:
                self._push_template_prices(template)
            except Exception as error:
                _logger.exception(
                    'Shopify pricing: failed for product %s', template.id)
                self._log('Price push failed for product %s (%s): %s' % (
                    template.display_name, template.shopify_product,
                    str(error)))
            else:
                self._cr.commit()

    # ------------------------------------------------------------------
    # queued job entry point
    # ------------------------------------------------------------------

    @api.model
    def export_pricing_to_shopify(self, data, instance):
        """Process a single queued pricing batch (called by job.cron._do_job).

        `data` is the Json payload stored on the job.cron record and holds the
        `template_ids` of this batch and the `pricelist_id` to price them
        with."""
        template_ids = data.get('template_ids', [])
        pricelist_id = data.get('pricelist_id')
        if not template_ids or not pricelist_id:
            return
        pricelist = self.env['product.pricelist'].sudo().browse(
            pricelist_id).exists()
        if not pricelist:
            _logger.warning(
                'Shopify pricing: pricelist %s no longer exists - batch '
                'skipped.', pricelist_id)
            return
        wizard = self.sudo().create({
            'shopify_instance_id': instance.id,
            'pricelist_id': pricelist.id,
        })
        templates = self.env['product.template'].sudo().browse(
            template_ids).exists()
        wizard._sync_to_shopify(templates)

    # ------------------------------------------------------------------
    # main action
    # ------------------------------------------------------------------

    def action_sync_pricing(self):
        """Queue the price push as job.cron batches."""
        self.ensure_one()
        if self.batch_size < 1:
            raise ValidationError(_('Batch size must be at least 1.'))

        templates = self._get_synced_templates()
        if not templates:
            raise ValidationError(_(
                'No product is synced with instance "%s". Import or export '
                'the products first, then push their prices.',
                self.shopify_instance_id.name))

        model = self.env['ir.model'].sudo().search(
            [('model', '=', 'sync.pricing')], limit=1)
        template_ids = templates.ids
        size = self.batch_size
        for index in range(0, len(template_ids), size):
            self.env['job.cron'].sudo().create([{
                'model_id': model.id,
                'function': 'export_pricing_to_shopify',
                'data': {
                    'template_ids': template_ids[index:index + size],
                    'pricelist_id': self.pricelist_id.id,
                },
                'instance_id': self.shopify_instance_id.id,
            }])

        batches = (len(template_ids) + size - 1) // size
        _logger.info(
            'Shopify pricing: queued %d product(s) in %d batch(es) for '
            'instance %s using pricelist %s',
            len(template_ids), batches, self.shopify_instance_id.name,
            self.pricelist_id.display_name)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Price Sync Queued'),
                'message': _(
                    '%(products)s product(s) queued in %(batches)s batch(es). '
                    'Prices are pushed by the Shopify job cron.',
                    products=len(template_ids), batches=batches),
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }

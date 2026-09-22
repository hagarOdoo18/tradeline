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
import functools
import json
import logging
import time

import requests

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

# Number of products sent in ONE GraphQL request. Each aliased
# productVariantsBulkUpdate costs ~10 points, so 25 stays far below the
# 1000-point single-query limit while cutting HTTP round-trips ~25x.
PRODUCTS_PER_REQUEST = 25
# Retries for HTTP 429 / 5xx and GraphQL THROTTLED responses.
MAX_RETRIES = 3
REQUEST_TIMEOUT = 60

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


# Current variants of one product, used to repair stale variant links when
# Shopify answers "Product variant does not exist".
PRODUCT_VARIANTS_QUERY = """
query productVariants($id: ID!) {
  product(id: $id) {
    id
    variants(first: 100) {
      nodes { id sku }
    }
  }
}
"""
VARIANT_MISSING = 'Product variant does not exist'


@functools.lru_cache(maxsize=None)
def _bulk_price_mutation(count):
    """Return one mutation document holding `count` aliased
    productVariantsBulkUpdate calls (p0, p1, ...), so a whole chunk of
    products is priced in a single HTTP request. Cached per size."""
    params = ', '.join(
        '$p%d: ID!, $v%d: [ProductVariantsBulkInput!]!' % (i, i)
        for i in range(count))
    body = '\n'.join(
        '  p%d: productVariantsBulkUpdate(productId: $p%d, variants: $v%d) '
        '{ userErrors { field message } }' % (i, i, i)
        for i in range(count))
    return 'mutation bulkPrices(%s) {\n%s\n}' % (params, body)


class SyncPricing(models.TransientModel):
    """Wizard to push each variant's own sales price to Shopify.

        The price sent is the variant price (`lst_price` = template sales
        price + attribute `price_extra`), not a pricelist price.

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
        string='Currency',
        default=lambda self: self.env.company.currency_id,
        readonly=True,
        help='Currency of the variant sales prices sent to Shopify',
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
        """Return the variant's own sales price.

        `lst_price` is the template sales price plus the attribute
        `price_extra` of the variant; no pricelist is involved."""
        currency = variant.currency_id or self.env.company.currency_id
        return float(currency.round(variant.lst_price or 0.0))

    def _log(self, message):
        """Write a log.message record for the current instance."""
        self.env['log.message'].sudo().create([{
            'name': message,
            'shopify_instance_id': self.shopify_instance_id.id,
            'model': 'product.product',
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

    @staticmethod
    def _post_graphql(session, url, body):
        """POST a GraphQL body, retrying on 429/5xx and THROTTLED."""
        result = {}
        for attempt in range(MAX_RETRIES + 1):
            response = session.post(url, data=body, timeout=REQUEST_TIMEOUT)
            if ((response.status_code == 429 or response.status_code >= 500)
                    and attempt < MAX_RETRIES):
                time.sleep(float(response.headers.get('Retry-After')
                                 or 2 ** attempt))
                continue
            if response.status_code != 200:
                raise ValidationError(_(
                    'Shopify returned HTTP %(code)s for the price update: '
                    '%(body)s',
                    code=response.status_code,
                    body=(response.text or '')[:500]))
            result = response.json()
            throttled = any(
                (error.get('extensions') or {}).get('code') == 'THROTTLED'
                for error in result.get('errors') or [])
            if throttled and attempt < MAX_RETRIES:
                cost = (result.get('extensions') or {}).get('cost') or {}
                status = cost.get('throttleStatus') or {}
                missing = ((cost.get('requestedQueryCost') or 0)
                           - (status.get('currentlyAvailable') or 0))
                time.sleep(max(1.0, missing / float(
                    status.get('restoreRate') or 50)))
                continue
            break
        return result

    def _template_price_syncs(self, template):
        """Variant-level shopify.sync rows of `template` for this instance
        that point to an Odoo variant (a row without one would push 0.00)."""
        instance = self.shopify_instance_id
        return template.shopify_sync_ids.filtered(
            lambda s: s.shopify_variant_id and s.product_prod_id
            and (not s.instance_id or s.instance_id == instance))

    def _relink_shopify_variants(self, template, session, url):
        """Re-read the product's variants from Shopify and re-point the
        Odoo links by SKU. Returns an error string, or None when relinked.

        Called only for products Shopify rejected with "Product variant does
        not exist": their stored variant ids are stale (product re-created on
        Shopify, variants re-generated, or a bad import) so this costs one
        extra query per broken product, once - the fixed links stick."""
        result = self._post_graphql(session, url, json.dumps({
            'query': PRODUCT_VARIANTS_QUERY,
            'variables': {
                'id': 'gid://shopify/Product/%s' % template.shopify_product},
        }))
        if result.get('errors'):
            return 'could not read its variants: %s' % json.dumps(
                result['errors'])
        product = (result.get('data') or {}).get('product')
        if not product:
            return ('Shopify product %s no longer exists - re-import or '
                    're-export this product' % template.shopify_product)
        live = [(node['id'].split('/')[-1], (node.get('sku') or '').strip())
                for node in (product.get('variants') or {}).get('nodes') or []]
        by_sku = {sku: vid for vid, sku in live if sku}

        odoo_variants = template.product_variant_ids
        matched = {}  # product.product id -> live Shopify variant id
        for variant in odoo_variants:
            for key in (variant.shopify_variant_sku, variant.default_code):
                key = (key or '').strip()
                if key and key in by_sku:
                    matched[variant.id] = by_sku[key]
                    break
        if not matched and len(live) == 1 and len(odoo_variants) == 1:
            matched[odoo_variants.id] = live[0][0]
        if not matched:
            return ('none of its Odoo variants matches a Shopify variant by '
                    'SKU (Shopify SKUs: %s)'
                    % (', '.join(sku for _vid, sku in live) or 'none'))

        instance = self.shopify_instance_id
        live_ids = {vid for vid, _sku in live}
        sync_obj = self.env['shopify.sync'].sudo()
        rows = sync_obj.search([
            '|', ('product_id', '=', template.id),
            ('product_prod_id', 'in', odoo_variants.ids),
            ('shopify_variant_id', '!=', False),
        ]).filtered(lambda s: not s.instance_id or s.instance_id == instance)
        linked = set()
        for row in rows:
            new_id = matched.get(row.product_prod_id.id)
            if new_id:
                if row.shopify_variant_id != new_id:
                    row.write({'shopify_variant_id': new_id,
                               'shopify_product': new_id})
                linked.add(row.product_prod_id.id)
            elif row.shopify_variant_id not in live_ids:
                # stale link with no Odoo counterpart: stop sending it
                row.write({'shopify_variant_id': False})
        sync_obj.create([{
            'instance_id': instance.id,
            'shopify_product': new_id,
            'shopify_variant_id': new_id,
            'product_prod_id': variant_id,
            'product_id': template.id,
        } for variant_id, new_id in matched.items() if variant_id not in linked])
        for variant in odoo_variants.filtered(lambda v: v.id in matched):
            if variant.shopify_variant != matched[variant.id]:
                variant.with_context(shopify_no_export=True).write(
                    {'shopify_variant': matched[variant.id]})
        template.invalidate_recordset(['shopify_sync_ids'])
        _logger.info('Shopify pricing: relinked %d variant(s) of "%s" to '
                     'Shopify product %s.', len(matched),
                     template.display_name, template.shopify_product)
        return None

    # ------------------------------------------------------------------
    # push
    # ------------------------------------------------------------------

    def _push_template_prices(self, templates, session, url, relink=True):
        """Push the prices of every synced variant of `templates` in ONE
        GraphQL request (one aliased mutation per product).

        Faster than one call per product because:
          * a single HTTP round-trip covers the whole chunk;
          * `session` keeps the TLS connection alive between chunks and
            carries the auth headers, so the token is not re-checked per call;
          * the response only asks for userErrors (no variant payload);
          * all log rows of the chunk are written with one create().
        """
        entries = []  # (template, [variant payload])
        for template in templates:
            # keyed by variant id: a duplicated id makes Shopify reject the
            # whole mutation
            payload = {}
            for sync in self._template_price_syncs(template):
                gid = ('gid://shopify/ProductVariant/%s'
                       % sync.shopify_variant_id)
                payload[gid] = {
                    'id': gid,
                    'price': '%.2f' % self._get_variant_price(
                        sync.product_prod_id),
                }
            if not payload:
                _logger.info(
                    'Shopify pricing: product "%s" has no variant linked to '
                    'a Shopify variant - skipped.', template.display_name)
                continue
            entries.append((template, list(payload.values())))
        if not entries:
            return

        variables = {}
        for index, (template, variants) in enumerate(entries):
            variables['p%d' % index] = ('gid://shopify/Product/%s'
                                        % template.shopify_product)
            variables['v%d' % index] = variants
        body = json.dumps({
            'query': _bulk_price_mutation(len(entries)),
            'variables': variables,
        })

        result = {}
        for attempt in range(MAX_RETRIES + 1):
            response = session.post(url, data=body, timeout=REQUEST_TIMEOUT)
            if ((response.status_code == 429 or response.status_code >= 500)
                    and attempt < MAX_RETRIES):
                time.sleep(float(response.headers.get('Retry-After')
                                 or 2 ** attempt))
                continue
            if response.status_code != 200:
                raise ValidationError(_(
                    'Shopify returned HTTP %(code)s for the price update: '
                    '%(body)s',
                    code=response.status_code,
                    body=(response.text or '')[:500]))
            result = response.json()
            throttled = any(
                (error.get('extensions') or {}).get('code') == 'THROTTLED'
                for error in result.get('errors') or [])
            if throttled and attempt < MAX_RETRIES:
                cost = (result.get('extensions') or {}).get('cost') or {}
                status = cost.get('throttleStatus') or {}
                missing = ((cost.get('requestedQueryCost') or 0)
                           - (status.get('currentlyAvailable') or 0))
                time.sleep(max(1.0, missing / float(
                    status.get('restoreRate') or 50)))
                continue
            break

        # Top level errors: those with a path belong to one alias (p<i>),
        # those without one sink the whole request.
        alias_errors, global_errors = {}, []
        for error in result.get('errors') or []:
            path = error.get('path') or []
            if path and str(path[0]).startswith('p'):
                alias_errors.setdefault(path[0], []).append(error)
            else:
                global_errors.append(error)
        data = result.get('data') or {}

        logs = []
        stale = []  # templates whose stored variant ids Shopify rejected
        instance_id = self.shopify_instance_id.id
        for index, (template, variants) in enumerate(entries):
            alias = 'p%d' % index
            errors = global_errors + alias_errors.get(alias, [])
            user_errors = (data.get(alias) or {}).get('userErrors') or []
            if (relink and not errors and user_errors and any(
                    error.get('message') == VARIANT_MISSING
                    for error in user_errors)):
                # the whole product mutation was rejected: nothing was
                # priced, so repair the links and push it again below
                stale.append((template, user_errors))
                continue
            if errors:
                message = 'Price push failed for product %s (%s): %s' % (
                    template.display_name, template.shopify_product,
                    json.dumps(errors))
            elif user_errors:
                message = 'Price push rejected for product %s (%s): %s' % (
                    template.display_name, template.shopify_product,
                    json.dumps(user_errors))
            else:
                message = ('Price push done for product %s (%s): '
                           '%s variant(s) - %s' % (
                               template.display_name,
                               template.shopify_product, len(variants),
                               ', '.join('%s=%s' % (v['id'].split('/')[-1],
                                                    v['price'])
                                         for v in variants)))
            logs.append({
                'name': message,
                'shopify_instance_id': instance_id,
                'model': 'product.product',
            })
        self._throttle(result)

        relinked = self.env['product.template']
        for template, user_errors in stale:
            problem = self._relink_shopify_variants(template, session, url)
            if problem:
                logs.append({
                    'name': 'Price push rejected for product %s (%s): %s - '
                            'stored variant ids are stale and %s' % (
                                template.display_name,
                                template.shopify_product,
                                json.dumps(user_errors), problem),
                    'shopify_instance_id': instance_id,
                    'model': 'product.product',
                })
            else:
                relinked |= template
        self.env['log.message'].sudo().create(logs)
        if relinked:
            # one more push with the repaired ids; relink=False so a product
            # that still fails is logged instead of looping
            self._push_template_prices(relinked, session, url, relink=False)

    def _sync_to_shopify(self, templates):
        """Push prices for the given templates, PRODUCTS_PER_REQUEST
        products per GraphQL call over one keep-alive session."""
        instance = self.shopify_instance_id
        url = 'https://%s/admin/api/%s/graphql.json' % (
            instance.shop_name, instance.version)
        # prefetch variants + prices for the whole batch in a few queries
        templates.mapped('product_variant_ids').mapped('lst_price')
        with requests.Session() as session:
            session.headers.update(instance._get_shopify_headers())
            for index in range(0, len(templates), PRODUCTS_PER_REQUEST):
                chunk = templates[index:index + PRODUCTS_PER_REQUEST]
                try:
                    self._push_template_prices(chunk, session, url)
                except Exception as error:
                    _logger.exception(
                        'Shopify pricing: failed for products %s', chunk.ids)
                    self.env['log.message'].sudo().create([{
                        'name': 'Price push failed for product %s (%s): %s'
                                % (template.display_name,
                                   template.shopify_product, str(error)),
                        'shopify_instance_id': instance.id,
                        'model': 'product.product',
                    } for template in chunk])
                self._cr.commit()

    # ------------------------------------------------------------------
    # queued job entry point
    # ------------------------------------------------------------------

    @api.model
    def export_pricing_to_shopify(self, data, instance):
        """Process a single queued pricing batch (called by job.cron._do_job).

        `data` is the Json payload stored on the job.cron record and holds the
        `template_ids` of this batch. Each variant is priced with its own
        sales price. A `pricelist_id` left in batches queued before this
        change is ignored."""
        template_ids = data.get('template_ids', [])
        if not template_ids:
            return
        wizard = self.sudo().create({
            'shopify_instance_id': instance.id,
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
                },
                'instance_id': self.shopify_instance_id.id,
            }])

        batches = (len(template_ids) + size - 1) // size
        _logger.info(
            'Shopify pricing: queued %d product(s) in %d batch(es) for '
            'instance %s using variant sales prices',
            len(template_ids), batches, self.shopify_instance_id.name)

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

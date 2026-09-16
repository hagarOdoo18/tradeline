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
import base64
import json
import logging
import requests
from odoo import api, models, fields, _
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class SyncProduct(models.TransientModel):
    """Class for transient model sync product."""
    _name = 'sync.product'
    _description = 'Sync Product'

    import_products = fields.Selection(string='Import/Export',
                                       selection=[('shopify', 'To Shopify'),
                                                  ('odoo', 'From Shopify')],
                                       required=True, default='odoo',
                                       help='Selection field to choose data'
                                            ' exchange type.')
    shopify_instance_id = fields.Many2one('shopify.configuration',
                                          string="Shopify Instance",
                                          required=True,
                                          help='Id of shopify instance')
    import_inventory = fields.Boolean(string='Import Inventory',
                                      help='Will import inventory of product'
                                           ' if true.')

    def _get_safe_barcode(self, barcode, exclude_id=None):
        """Return barcode only if it is not already assigned to another
        product. Returns None if duplicate found to avoid ValidationError."""
        if not barcode:
            return None
        domain = [('barcode', '=', barcode)]
        if exclude_id:
            domain.append(('id', '!=', exclude_id))
        existing = self.env['product.product'].sudo().search(domain, limit=1)
        if existing:
            _logger.warning(
                'Skipping duplicate barcode %s already assigned to %s',
                barcode, existing.display_name)
            return None
        return barcode

    @api.model
    def _primary_variant_for_sku(self, candidates, sku):
        """The variant that *owns* `sku`, out of everything matching it.

        The same code can sit in `shopify_variant_sku` on one variant and in
        `barcode` on another, possibly on another template. The
        `shopify_variant_sku` match wins: it is set deliberately to name this
        item on Shopify, while a barcode is generic data that may be shared
        or reused. Ties break on the lowest id.

        This is the same priority as
        ``sync.inventory._inventory_group_key``::

            code = shopify_variant_sku or barcode

        Keep the two in step - if one ranks the codes differently from the
        other, a product can bind to one template here and be grouped under
        another when its stock is pushed.

        Without this the winner was `candidates[0]`, i.e. whatever the search
        happened to return first - so which Odoo template a Shopify product
        bound to depended on product ordering, and could flip when someone
        edited a name or an internal reference.
        """
        if not candidates:
            return candidates
        code = (sku or '').strip()
        by_sku = candidates.filtered(
            lambda variant: (variant.shopify_variant_sku or '').strip() == code)
        return (by_sku or candidates).sorted('id')[0]

    @api.model
    def _cron_import_products_from_shopify(self):
        """Scheduled action to import products from Shopify for all active
        connected instances. For each instance a transient sync.product
        record is created in the 'From Shopify' direction and
        action_sync_products is called, which fetches the products
        (paginated) and queues import_products_from_shopify job.cron records
        that _do_job then processes."""
        instances =  self.env['shopify.configuration'].search(
                    [('company_id', '=', self.env.company.id)])
        for instance in instances:
            try:
                wizard = self.sudo().create({
                    'import_products': 'odoo',
                    'shopify_instance_id': instance.id,
                })
                wizard.action_sync_products()
            except Exception as error:
                _logger.error(
                    'Failed to queue product import for Shopify instance '
                    '%s: %s', instance.name, str(error))

    def action_sync_products(self):
        """Method to create queue jobs for exporting and importing data."""
        model = self.env['ir.model'].search([('model', '=', "sync.product")])
        if (self.import_products == 'shopify' and
                not self.shopify_instance_id.export_product):
            raise ValidationError(_(
                'To synchronize products with Shopify, activate the "Export '
                'Product" feature in the Shopify configuration.'))
        shopify_instance = self.shopify_instance_id
        store_name = shopify_instance.shop_name
        version = shopify_instance.version
        headers = shopify_instance._get_shopify_headers()
        if self.import_products == 'shopify':
            products = self.env['product.template'].search([])
            product_list = []
            product_id_list = []
            size = 50
            for i in range(0, len(products), size):
                product_list.append(products[i:i + size])
            for product in product_list:
                for item in product:
                    if self.shopify_instance_id.id \
                            not in item.shopify_sync_ids.ids:
                        product_id_list.append(item.id)
                self.env['job.cron'].sudo().create([{
                    'model_id': model.id,
                    'function': "export_products_to_shopify",
                    'data': product_id_list,
                    'instance_id': self.shopify_instance_id.id
                }])
                product_id_list = []
        else:
            # limit=50 matches Shopify default page size
            next_url = 'https://%s/admin/api/%s/products.json?limit=50' % (
                store_name, version)
            while next_url:
                response = requests.request('GET', next_url,verify=False,
                                            headers=headers, data=[])
                response_json = response.json()
                if 'products' in response_json and response_json['products']:
                    self.env['job.cron'].sudo().create([{
                        'model_id': model.id,
                        'function': "import_products_from_shopify",
                        'data': response_json['products'],
                        'instance_id': self.shopify_instance_id.id,
                    }])
                    _logger.info(
                        'Shopify product sync: queued %d products',
                        len(response_json['products']))
                # Parse next page URL cleanly from Link header
                next_url = None
                link_header = response.headers.get('link', '')
                for part in link_header.split(','):
                    part = part.strip()
                    if 'rel="next"' in part:
                        start = part.find('<') + 1
                        end = part.find('>')
                        if start > 0 and end > start:
                            next_url = part[start:end]
                        break

    def import_products_from_shopify(self, shopify_products, instance):
        """Method to import products from shopify to odoo.

        Matches existing products by SKU instead of creating new ones.

        A Shopify SKU is matched against `barcode` and `shopify_variant_sku`,
        so it finds every variant carrying the code. The
        **shopify_variant_sku** match decides which template the Shopify
        product binds to - the same priority the inventory grouping uses (see
        :meth:`_primary_variant_for_sku`); variants living on another template
        are linked as well, each carrying the Shopify ids on its own template,
        so no Odoo product that is already on Shopify is ever left looking
        unsynced - which is what made `export_products_to_shopify` create a
        duplicate listing for it.

        A Shopify product that already has shopify.sync rows for this
        instance is NOT skipped: it is matched again and its sync rows
        (template and variant level) are deleted and re-created from the
        current Shopify payload, so the links are always rebuilt. Products
        that match no Odoo SKU keep the links they had.

        All lookups for the whole page are batched: two queries for the
        entire batch instead of ~3 queries per product, and one create for
        all the shopify.sync records. Writes are done with
        ``shopify_no_export=True`` so that importing never triggers the
        outbound Shopify PUT in ``product.template.write()``.
        """
        shopify_instance = instance
        if not shopify_products:
            return

        product_obj = self.env['product.product'].sudo()
        sync_obj = self.env['shopify.sync'].sudo()

        # ٢. تجميع كل الـ SKUs في الدفعة والبحث عنها مرة واحدة
        all_skus = set()
        for product in shopify_products:
            for shopify_var in product.get('variants') or []:
                if shopify_var.get('sku'):
                    all_skus.add(shopify_var['sku'])

        variants_by_sku = {}
        if all_skus:
            sku_list = list(all_skus)
            matching = product_obj.search([
                '|',
                ('barcode', 'in', sku_list),
                ('shopify_variant_sku', 'in', sku_list),
            ])
            for odoo_variant in matching:
                for key in (odoo_variant.barcode,
                            odoo_variant.shopify_variant_sku):
                    if key and key in all_skus:
                        variants_by_sku[key] = (
                            variants_by_sku.get(key, product_obj)
                            | odoo_variant)

        sync_vals_list = []
        # (shopify_variant_id, product_product_id) already queued: two Shopify
        # variants sharing one SKU must not create the same link twice.
        seen_variant_links = set()

        for product in shopify_products:
            # ٠. تخطي المنتجات التي ليس لها variants في شوبيفاي
            if not product.get('variants'):
                _logger.warning(
                    'Shopify product "%s" (id=%s) has no variants — skipped.',
                    product.get('title'), product.get('id')
                )
                continue

            shopify_skus = [
                v['sku'] for v in product['variants'] if v.get('sku')
            ]

            # ٣. البحث عن product.template موجود بأي SKU من الـ variants
            #    الأولوية لمطابقة shopify_variant_sku ثم الباركود،
            #    وليس ترتيب البحث
            product_id = None
            for sku in shopify_skus:
                primary = self._primary_variant_for_sku(
                    variants_by_sku.get(sku), sku)
                if primary:
                    product_id = primary.product_tmpl_id
                    break

            # ٤. لو ما لقيناش منتج مطابق → skip
            if not product_id:
                _logger.warning(
                    'Shopify product "%s" (id=%s): no matching Odoo product '
                    'found by SKU %s — skipped.',
                    product.get('title'), product.get('id'), shopify_skus
                )
                continue

            # ٥. ربط كل variant بنظيره في أودو عبر الـ SKU
            #
            # An alias variant (same code in `shopify_variant_sku`) may live on
            # another template. It is linked too, instead of being dropped:
            # its own template then carries the Shopify id and a shopify.sync
            # row, so `export_products_to_shopify` no longer sees it as a new
            # product and cannot create a duplicate listing on Shopify.
            linked_templates = product_id
            for shopify_var in product['variants']:
                sku = shopify_var.get('sku')
                if not sku:
                    continue

                odoo_variants = variants_by_sku.get(sku, product_obj)

                if not odoo_variants:
                    _logger.warning(
                        'Shopify variant SKU "%s" not found in product "%s" '
                        '— skipped.', sku, product_id.name
                    )
                    continue

                foreign = odoo_variants.filtered(
                    lambda v, tmpl=product_id: v.product_tmpl_id != tmpl)
                if foreign:
                    _logger.info(
                        'Shopify variant SKU "%s" also matches %s on other '
                        'template(s) than "%s" — linking them as aliases.',
                        sku, foreign.mapped('display_name'), product_id.name)

                # Re-syncing usually finds these values already correct;
                # writing only the records that actually change keeps a
                # full re-import from costing one ORM write per variant.
                variant_vals = {
                    'shopify_variant': shopify_var['id'],
                    'shopify_instance_id': shopify_instance.id,
                    'synced_product': True,
                }
                outdated_variants = odoo_variants.filtered(
                    lambda v, sid=str(shopify_var['id']): (
                        str(v.shopify_variant or '') != sid
                        or v.shopify_instance_id.id != shopify_instance.id
                        or not v.synced_product))
                if outdated_variants:
                    outdated_variants.sudo().with_context(
                        shopify_no_export=True).write(variant_vals)

                for odoo_variant in odoo_variants:
                    link = (str(shopify_var['id']), odoo_variant.id)
                    if link in seen_variant_links:
                        continue
                    seen_variant_links.add(link)
                    sync_vals_list.append({
                        'instance_id': shopify_instance.id,
                        'shopify_product': shopify_var['id'],
                        'shopify_variant_id': shopify_var['id'],
                        'product_prod_id': odoo_variant.id,
                        # the alias's OWN template, so that template is the
                        # one that counts as synced
                        'product_id': odoo_variant.product_tmpl_id.id,
                    })
                    linked_templates |= odoo_variant.product_tmpl_id

            # ٦. ربط كل الـ templates المعنية بالـ instance
            #    (فقط التي تختلف قيمها فعلاً)
            outdated_templates = linked_templates.filtered(
                lambda t, pid=str(product['id']): (
                    str(t.shopify_product or '') != pid
                    or t.shopify_instance_id.id != shopify_instance.id
                    or not t.synced_product))
            if outdated_templates:
                outdated_templates.sudo().with_context(
                    shopify_no_export=True).write({
                        'shopify_product': product['id'],
                        'shopify_instance_id': shopify_instance.id,
                        'synced_product': True,
                    })

            # ٧. تسجيل في shopify.sync على مستوى الـ template
            for template in linked_templates:
                sync_vals_list.append({
                    'instance_id': shopify_instance.id,
                    'shopify_product': product['id'],
                    'product_id': template.id,
                })

        # ٨. حذف الروابط القديمة ثم إنشاء الجديدة دفعة واحدة
        #
        # Only the Shopify ids that are being re-created are deleted, so a
        # product that matched nothing in Odoo keeps whatever links it had:
        # stripping those would make its template look unsynced and
        # `export_products_to_shopify` would push it to Shopify again as a
        # new listing.
        #
        # `shopify_product` alone is queried: it is the indexed column and
        # holds the product id on template rows and the variant id on
        # variant rows, so one indexed IN removes both - no scan of the
        # unindexed `shopify_variant_id`.
        if sync_vals_list:
            resync_ids = list({
                str(vals['shopify_product']) for vals in sync_vals_list
            })
            stale_syncs = sync_obj.search([
                ('instance_id', '=', shopify_instance.id),
                ('shopify_product', 'in', resync_ids),
            ])
            if stale_syncs:
                _logger.info(
                    'Shopify product import: removed %d existing '
                    'shopify.sync record(s) before re-linking.',
                    len(stale_syncs))
                stale_syncs.unlink()
            sync_obj.create(sync_vals_list)
        _logger.info('Shopify product import: processed %d products, '
                     'created %d shopify.sync records.',
                     len(shopify_products), len(sync_vals_list))

    def export_products_to_shopify(self, lists, instance):
        """Method to export products from odoo to shopify."""
        store_name = instance.shop_name
        version = instance.version
        product_url = 'https://%s/admin/api/%s/products.json' % (
            store_name, version)
        headers = instance._get_shopify_headers()
        product = self.env['product.template'].sudo().search(
            [('id', 'in', lists)])
        product.synced_product = False
        for rec in product:
            if rec.sale_ok:
                instance_ids = rec.shopify_sync_ids.mapped('instance_id.id')
                if instance.id not in instance_ids:
                    variants = []
                    for line in rec.product_variant_ids:
                        line_vals = {
                            'option1': line.partner_ref,
                            'price': line.lst_price,
                            'sku': line.default_code if line.default_code
                            else None,
                            'inventory_quantity': int(line.qty_available),
                            'barcode': line.barcode if line.barcode else None,
                            'id': rec.id,
                            'product_id': rec.id,
                        }
                        variants.append(line_vals)
                    if not variants:
                        line_vals = {
                            'id': rec.id,
                            'product_id': rec.id,
                            'title': rec.name,
                            'body_html': rec.description_sale
                            if rec.description_sale else '',
                            'price': rec.list_price,
                            'sku': rec.default_code if rec.default_code
                            else None,
                            'inventory_quantity': int(rec.qty_available),
                            'unitCost': rec.standard_price,
                            'product_type': 'Consumable'
                            if rec.type == 'consu' else 'Service',
                        }
                        variants.append(line_vals)
                    payload = json.dumps({
                        'product': {
                            'id': rec.id,
                            'title': rec.name,
                            'body_html': rec.description_sale
                            if rec.description_sale else '',
                            'sku': rec.default_code if rec.default_code
                            else None,
                            'inventory_quantity': int(rec.qty_available),
                            'barcode': rec.barcode if rec.barcode else None,
                            'product_type': 'Consumable'
                            if rec.type == 'consu' else 'Service',
                            'unitCost': rec.standard_price,
                            'variants': variants,
                        }
                    })
                    response = requests.request('POST', product_url,
                                                headers=headers, data=payload)
                    if response.status_code != 201:
                        self.env['log.message'].sudo().create([{
                            'name': 'Exporting Product: ' + rec.name +
                                    ' is not processed. ' +
                                    str(response.json().get('errors', '')),
                            'shopify_instance_id': instance.id,
                            'model': 'product.template',
                        }])
                        continue
                    response_rec = response.json()
                    response_product_id = response_rec['product']['id']
                    rec.shopify_sync_ids.sudo().create({
                        'instance_id': instance.id,
                        'shopify_product': response_product_id,
                        'product_id': rec.id,
                    })
                    for prod_variants, items in zip(
                            rec.product_variant_ids,
                            response_rec['product']['variants']):
                        prod_variants.shopify_sync_ids.sudo().create({
                            'instance_id': instance.id,
                            'shopify_product': items['id'],
                            'product_prod_id': prod_variants.id,
                        })

    def create_product_by_id(self, shopify_instance, store_name, version,
                             product):
        """Method to create a specific shopify product to odoo by product id."""
        product_tags = []
        product_url = 'https://%s/admin/api/%s/products/%s.json' % (
            store_name, version, product)
        headers = shopify_instance._get_shopify_headers()
        response = requests.request('GET', product_url,
                                    headers=headers, data=[])
        if 'product' in response.json():
            product = response.json()['product']
            if product['options']:
                for option in product['options']:
                    attribute_id = self.env['product.attribute'].sudo(
                    ).search([
                        ('shopify_attribute', '=', option['id']),
                        ('shopify_instance_id', '=', shopify_instance.id)
                    ])
                    if attribute_id:
                        for opt_val in option['values']:
                            val = attribute_id.value_ids.filtered(
                                lambda x: x.name == opt_val)
                            if not val:
                                attribute_id.sudo().write({
                                    'value_ids': [(0, 0, {'name': opt_val})]
                                })
                    else:
                        attribute_id = self.env['product.attribute'].sudo(
                        ).create([{
                            'name': option['name'],
                            'shopify_attribute': option['id'],
                            'shopify_instance_id': shopify_instance.id,
                        }])
                        for opt_val in option['values']:
                            attribute_id.sudo().write({
                                'value_ids': [(0, 0, {'name': opt_val})]
                            })
            if product['tags']:
                tags = product['tags'].split(',')
                for rec in tags:
                    product_tag = self.env['product.tag'].search(
                        [('name', '=', rec)])
                    if product_tag:
                        product_tags.append(product_tag.id)
                    else:
                        product_tag = self.env['product.tag'].create([{
                            'name': rec,
                        }])
                        product_tags.append(product_tag.id)
            product_id = self.env['product.template'].sudo().create([{
                'name': product['title'],
                'type': 'consu',
                'categ_id': self.env.ref('product.product_category_all').id,
                'synced_product': True,
                'default_code': product['variants'][0]['sku'] if
                product['variants'][0]['sku'] else None,
                'description': product['body_html'],
                'shopify_product': product['id'],
                'shopify_instance_id': shopify_instance.id,
                'company_id': shopify_instance.company_id.id,
            }])
            product_id.shopify_sync_ids.sudo().create({
                'instance_id': shopify_instance.id,
                'shopify_product': product['id'],
                'product_id': product_id.id,
            })
            shopify_price_list = []
            if product['options']:
                for option in product['options']:
                    attribute_id = self.env[
                        'product.attribute'].sudo().search([
                            ('shopify_attribute', '=', option['id']),
                            ('shopify_instance_id', '=', shopify_instance.id)
                        ])
                    att_val_ids = self.env[
                        'product.attribute.value'].sudo().search([
                            ('name', 'in', option['values']),
                            ('attribute_id', '=', attribute_id.id)
                        ])
                    att_line = {
                        'attribute_id': attribute_id.id,
                        'value_ids': [(4, att_val.id) for att_val in
                                      att_val_ids]
                    }
                    product_id.sudo().write({
                        'attribute_line_ids': [(0, 0, att_line)]
                    })
                for shopify_var in product['variants']:
                    shopify_var_list = []
                    shopify_var_id_list = []
                    for i in range(1, 3):
                        if shopify_var['option' + str(i)] is not None:
                            shopify_var_list.append(
                                shopify_var['option' + str(i)])
                        else:
                            break
                    for option in product['options']:
                        for var in shopify_var_list:
                            if var in option['values']:
                                attribute_id = self.env[
                                    'product.attribute'].sudo().search([
                                        ('shopify_attribute', '=', option['id']),
                                        ('shopify_instance_id', '=',
                                         shopify_instance.id)
                                    ])
                                att_val_id = attribute_id.value_ids.filtered(
                                    lambda x: x.name == var)
                                shopify_var_id_list.append(att_val_id)
                    for variant in product_id.product_variant_ids:
                        o_var_list = (
                            variant.product_template_variant_value_ids.mapped(
                                'product_attribute_value_id'))
                        o_var_list = [rec for rec in o_var_list]
                        if o_var_list == shopify_var_id_list:
                            variant.sudo().write({
                                'shopify_variant': shopify_var['id'],
                                'shopify_instance_id': shopify_instance.id,
                                'synced_product': True,
                                'company_id': shopify_instance.company_id.id,
                                'default_code': shopify_var['sku'] if
                                shopify_var['sku'] else None,
                                'barcode': self._get_safe_barcode(
                                    shopify_var['barcode'], variant.id),
                                'lst_price': shopify_var['price'],
                            })
                            price_dict = {
                                shopify_var['id']: shopify_var['price'],
                                'variant': shopify_var['title']
                            }
                            shopify_price_list.append(price_dict)
                            variant.shopify_sync_ids.sudo().create({
                                'instance_id': shopify_instance.id,
                                'shopify_product': shopify_var['id'],
                                'product_prod_id': variant.id,
                            })
                        elif (not o_var_list and
                              shopify_var['option2'] is None and
                              len(product_id.product_variant_ids) == 1):
                            variant.sudo().write({
                                'shopify_variant': shopify_var['id'],
                                'shopify_instance_id': shopify_instance.id,
                                'synced_product': True,
                                'company_id': shopify_instance.company_id.id,
                                'default_code': shopify_var['sku'] if
                                shopify_var['sku'] else None,
                                'barcode': self._get_safe_barcode(
                                    shopify_var['barcode'], variant.id),
                                'lst_price': shopify_var['price'],
                            })
                            price_dict = {
                                shopify_var['id']: shopify_var['price'],
                                'variant': shopify_var['title']
                            }
                            shopify_price_list.append(price_dict)
                            variant.shopify_sync_ids.sudo().create({
                                'instance_id': shopify_instance.id,
                                'shopify_product': shopify_var['id'],
                                'product_prod_id': variant.id,
                            })
                    for image in product['images']:
                        variant = self.env['product.product'].search(
                            [('shopify_product', '=', image['product_id'])])
                        src = image['src']
                        image_1920 = base64.b64encode(
                            requests.get(src).content)
                        for i in variant:
                            i.write({'image_1920': image_1920})
                for rec in shopify_price_list:
                    product_product_id = self.env[
                        'product.product'].sudo().search(
                        [('shopify_variant', '=', list(rec.keys())[0])])
                    product_attribute_id = self.env[
                        'product.template.attribute.value'].sudo().search(
                        [('ptav_product_variant_ids', '=',
                          product_product_id.id)])
                    default_price = product_product_id.lst_price
                    extra_price = float(list(rec.values())[0]) - default_price
                    product_attribute_id.sudo().write({
                        'price_extra': float(extra_price)
                    })
            else:
                for variant in product_id.product_variant_ids:
                    variant.sudo().write({
                        'shopify_variant': product['id'],
                        'shopify_instance_id': shopify_instance.id,
                        'synced_product': True,
                        'company_id': shopify_instance.company_id.id,
                    })
        return response.json()

# -*- coding: utf-8 -*-

from odoo import models, fields, api,_
from odoo.osv import expression
import itertools
from odoo.tools import mute_logger, unique, lazy

import logging
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

from odoo.exceptions import ValidationError
class ProductTemplateInherit(models.Model):
    _inherit = 'product.template'

    name = fields.Char('Name', index=True, required=True, translate=True)

    family_id = fields.Many2one(comodel_name='product.family', string='Product Family')
    sub_categ_id = fields.Many2one(comodel_name='sub.category', string='Sub Category')
    default_code = fields.Char('UPC', index=True)

    @api.constrains('taxes_id')
    def _check_taxes_required(self):
        for product in self:
            if not product.taxes_id:
                raise ValidationError(
                    "Tax is required on product '%s'. "
                    "Please set a customer tax before saving." % product.name
                )

    def _create_variant_ids(self):
        if not self:
            return
        self.env.flush_all()
        Product = self.env["product.product"]

        variants_to_create = []
        variants_to_activate = Product
        variants_to_unlink = Product

        for tmpl_id in self:
            lines_without_no_variants = tmpl_id.valid_product_template_attribute_line_ids._without_no_variant_attributes()

            all_variants = tmpl_id.with_context(active_test=False).product_variant_ids.sorted(lambda p: (p.active, -p.id))

            current_variants_to_create = []
            current_variants_to_activate = Product

            # adding an attribute with only one value should not recreate product
            # write this attribute on every product to make sure we don't lose them
            single_value_lines = lines_without_no_variants.filtered(lambda ptal: len(ptal.product_template_value_ids._only_active()) == 1)
            if single_value_lines:
                for variant in all_variants:
                    combination = variant.product_template_attribute_value_ids | single_value_lines.product_template_value_ids._only_active()
                    # Do not add single value if the resulting combination would
                    # be invalid anyway.
                    if (
                        len(combination) == len(lines_without_no_variants) and
                        combination.attribute_line_id == lines_without_no_variants
                    ):
                        variant.product_template_attribute_value_ids = combination

            # Set containing existing `product.template.attribute.value` combination
            existing_variants = {
                variant.product_template_attribute_value_ids: variant for variant in all_variants
            }

            # Determine which product variants need to be created based on the attribute
            # configuration. If any attribute is set to generate variants dynamically, skip the
            # process.
            # Technical note: if there is no attribute, a variant is still created because
            # 'not any([])' and 'set([]) not in set([])' are True.
            if not tmpl_id.has_dynamic_attributes():
                # Iterator containing all possible `product.template.attribute.value` combination
                # The iterator is used to avoid MemoryError in case of a huge number of combination.
                all_combinations = itertools.product(*[
                    ptal.product_template_value_ids._only_active() for ptal in lines_without_no_variants
                ])
                # For each possible variant, create if it doesn't exist yet.
                for combination in tmpl_id._filter_combinations_impossible_by_config(
                    all_combinations, ignore_no_variant=True,
                ):
                    if combination in existing_variants:
                        current_variants_to_activate += existing_variants[combination]
                        for pp in current_variants_to_activate:
                            if not pp.vendor_id:


                                pp.vendor_id =  combination.product_attribute_value_id.vendor_id.id,
                    else:
                        current_variants_to_create.append(tmpl_id._prepare_variant_values(combination))
                        variant_limit = self.env['ir.config_parameter'].sudo().get_param('product.dynamic_variant_limit', 1000)
                        if len(current_variants_to_create) > int(variant_limit):
                            raise UserError(_(
                                'The number of variants to generate is above allowed limit. '
                                'You should either not generate variants for each combination or generate them on demand from the sales order. '
                                'To do so, open the form view of attributes and change the mode of *Create Variants*.'))
                variants_to_create += current_variants_to_create
                variants_to_activate += current_variants_to_activate

            elif existing_variants:
                variants_combinations = [variant.product_template_attribute_value_ids for variant in existing_variants.values()]
                current_variants_to_activate += Product.concat(*[existing_variants[possible_combination]
                    for possible_combination in tmpl_id._filter_combinations_impossible_by_config(variants_combinations, ignore_no_variant=True)
                ])
                variants_to_activate += current_variants_to_activate

            variants_to_unlink += all_variants - current_variants_to_activate

        if variants_to_activate:
            variants_to_activate.write({'active': True})
        if variants_to_create:
            Product.create(variants_to_create)
        if variants_to_unlink:
            variants_to_unlink._unlink_or_archive()
            # prevent change if exclusion deleted template by deleting last variant
            if self.exists() != self:
                raise UserError(_("This configuration of product attributes, values, and exclusions would lead to no possible variant. Please archive or delete your product directly if intended."))
        for variant in variants_to_unlink:
            combo_items_to_unlink = self.env['product.combo.item'].search([
                ('product_id', '=', variant.id)
            ])
            # Unlink all combo items which reference unlinked variants.
            combo_items_to_unlink.unlink()

        # prefetched o2m have to be reloaded (because of active_test)
        # (eg. product.template: product_variant_ids)
        # We can't rely on existing invalidate because of the savepoint
        # in _unlink_or_archive.
        self.env.flush_all()
        self.env.invalidate_all()
        return True
    def _prepare_variant_values(self, combination):
        variant_dict = super()._prepare_variant_values(combination)
        if 'vendor_id' in variant_dict:

            variant_dict['vendor_id'] = combination.product_attribute_value_id.vendor_id.id,

        return variant_dict




class StockValuationLayer(models.Model):
    _inherit = 'stock.valuation.layer'

    lst_price = fields.Float(
        string="Original Price",
        digits='Product Price',
        related='product_id.lst_price',
    )




class Serial(models.Model):
    _inherit = 'stock.lot'


    def write(self, values):
        # Add code here
        if 'name' in values:
            if not self.env.user.has_group('inventory_customization.group_product_serial_admin'):
                raise UserError("Not Allowed TO Edit Name")

        return super(Serial, self).write(values)

class ProductProduct(models.Model):
    _inherit = 'product.product'


    @api.model
    def action_update_vendor(self):
        """Update vendor for selected products"""

        for rec in self:
            rec.write({'vendor_id': 7755})

    vendor_id = fields.Many2one(
        comodel_name='res.partner',
        string='Vendor',
        required=False)
    default_code = fields.Char('UPC', index=True)

    barcode = fields.Char(
        'Item Code', copy=False, oldname='ean13',
        help="International Article Number used for product identification.", required=True)

    product_point = fields.Float(
        string='Product point', company_dependent=True, check_company=True,
        required=False)

    product_incentive = fields.Float(
        string='Product Incentive', company_dependent=True, check_company=True,
        required=False)

    product_notes = fields.Char(
        string='Product Notes',
        required=False)
    @api.depends('name', 'default_code', 'product_tmpl_id')
    @api.depends_context('display_default_code', 'seller_id', 'company_id', 'partner_id')
    def _compute_display_name(self):

        def get_display_name(name, code):

            return name

        partner_id = self._context.get('partner_id')
        if partner_id:
            partner_ids = [partner_id, self.env['res.partner'].browse(partner_id).commercial_partner_id.id]
        else:
            partner_ids = []
        company_id = self.env.context.get('company_id')

        # all user don't have access to seller and partner
        # check access and use suproduct_tmpl_idperuser
        self.check_access("read")

        product_template_ids = self.sudo().product_tmpl_id.ids

        if partner_ids:
            # prefetch the fields used by the `display_name`
            supplier_info = self.env['product.supplierinfo'].sudo().search_fetch(
                [('product_tmpl_id', 'in', product_template_ids), ('partner_id', 'in', partner_ids)],
                ['product_tmpl_id', 'product_id', 'company_id', 'product_name', 'product_code'],
            )
            supplier_info_by_template = {}
            for r in supplier_info:
                supplier_info_by_template.setdefault(r.product_tmpl_id, []).append(r)

        for product in self.sudo():
            product_template_attribute_value_ids= []
            for p in product.product_template_attribute_value_ids:
                if not p.product_attribute_value_id.attribute_id.is_vendor:
                    product_template_attribute_value_ids.append(p.id)
            if product_template_attribute_value_ids:
                variant = self.env['product.template.attribute.value'].browse(product_template_attribute_value_ids)._get_combination_name()

                name = variant and "%s %s" % (product.name ,variant)or product.name
            else:
                name = product.name

            sellers = self.env['product.supplierinfo'].sudo().browse(self.env.context.get('seller_id')) or []
            if not sellers and partner_ids:
                product_supplier_info = supplier_info_by_template.get(product.product_tmpl_id, [])
                sellers = [x for x in product_supplier_info if x.product_id and x.product_id == product]
                if not sellers:
                    sellers = [x for x in product_supplier_info if not x.product_id]
                # Filter out sellers based on the company. This is done afterwards for a better
                # code readability. At this point, only a few sellers should remain, so it should
                # not be a performance issue.
                if company_id:
                    sellers = [x for x in sellers if x.company_id.id in [company_id, False]]
            if sellers:
                temp = []
                for s in sellers:
                    seller_variant = s.product_name and (
                            variant and "%s (%s)" % (s.product_name, variant) or s.product_name
                    ) or False
                    temp.append(get_display_name(seller_variant or name, s.product_code or product.default_code))

                # => Feature drop here, one record can only have one display_name now, instead separate with `,`
                # Remove this comment
                product.display_name = ", ".join(unique(temp))
            else:
                product.display_name = get_display_name(name, product.default_code)

    def name_get(self):
        # TDE: this could be cleaned a bit I think

        def _name_get(d):
            name = d.get('name', '')
            code = d.get('barcode', False) or False
            if code:
                name = '[%s] %s' % (code, name)
            return (d['id'], name)

        partner_id = self._context.get('partner_id')
        if partner_id:
            partner_ids = [partner_id, self.env['res.partner'].browse(partner_id).commercial_partner_id.id]
        else:
            partner_ids = []

        # all user don't have access to seller and partner
        # check access and use superuser
        self.check_access_rights("read")
        self.check_access_rule("read")

        result = []

        # Prefetch the fields used by the `name_get`, so `browse` doesn't fetch other fields
        # Use `load=False` to not call `name_get` for the `product_tmpl_id`
        self.sudo().read(['name', 'barcode', 'product_tmpl_id', 'product_template_attribute_value_ids'],
                         load=False)

        product_template_ids = self.sudo().mapped('product_tmpl_id').ids

        if partner_ids:
            supplier_info = self.env['product.supplierinfo'].sudo().search([
                ('product_tmpl_id', 'in', product_template_ids),
                ('name', 'in', partner_ids),
            ])
            # Prefetch the fields used by the `name_get`, so `browse` doesn't fetch other fields
            # Use `load=False` to not call `name_get` for the `product_tmpl_id` and `product_id`
            supplier_info.sudo().read(['product_tmpl_id', 'product_id', 'product_name', 'product_code'], load=False)
            supplier_info_by_template = {}
            for r in supplier_info:
                supplier_info_by_template.setdefault(r.product_tmpl_id, []).append(r)
        for product in self.sudo():
            # display only the attributes with multiple possible values on the template
            variant = product.product_template_attribute_value_ids._get_combination_name()

            name = variant and "%s (%s)" % (product.name, variant) or product.name
            sellers = []
            lots = []
            # if product.is_storable:

            if partner_ids:
                product_supplier_info = supplier_info_by_template.get(product.product_tmpl_id, [])
                sellers = [x for x in product_supplier_info if x.product_id and x.product_id == product]
                if not sellers:
                    sellers = [x for x in product_supplier_info if not x.product_id]
            if sellers:
                for s in sellers:
                    seller_variant = s.product_name and (
                            variant and "%s (%s)" % (s.product_name, variant) or s.product_name
                    ) or False
                    mydict = {
                        'id': product.id,
                        'name': seller_variant or name,
                        'barcode': s.product_code or product.barcode,
                    }
                    temp = _name_get(mydict)
                    if temp not in result:
                        result.append(temp)
            else:
                mydict = {
                    'id': product.id,
                    'name': name,
                    'barcode': product.barcode,
                }
                result.append(_name_get(mydict))
        return result

    @api.model
    def name_search(self, name='', args=None, operator='ilike', limit=100):
        args = args or []
        domain = []

        if name:
            # 1. Custom Variant Aware Search Logic
            # Split the string (e.g., 'Apple iPhone 16e 128GB, White')
            pieces = [p.strip() for p in name.replace(',', ' ').split() if p.strip()]
            for piece in pieces:
                domain.append('|')
                domain.append(('name', operator, piece))
                domain.append(('product_template_attribute_value_ids.name', operator, piece))
            
            # Combine the custom domain with any core args passed in
            domain = expression.AND([domain, args])
            
            # 2. Add custom logic: if name matches a Lot/Serial Number, include its product
            lot_products = self.env['stock.lot'].search([('name', '=', name)]).mapped('product_id')
            if lot_products:
                domain = expression.OR([domain, [('id', 'in', lot_products.ids)]])
                
            # Execute the search on our custom domain
            products = self.search(domain, limit=limit)
            return products.name_get()

        # If no name provided, fallback to standard behavior
        return super(ProductProduct, self).name_search(name=name, args=args, operator=operator, limit=limit)


class ProductTemplateAttributeValue(models.Model):
    """Materialized relationship between attribute values
    and product template generated by the product.template.attribute.line"""

    _inherit = 'product.template.attribute.value'
    def _without_no_variant_attributes(self):
        return self.filtered(lambda ptav: ptav.attribute_id.create_variant != 'no_variant'  and  ptav.attribute_id.create_variant != 'vendor_id'  )

class ProductTemplateAttributeLine(models.Model):

    _inherit = 'product.template.attribute.line'

    @api.model
    def action_update_vendor(self):
        """Update vendor for selected products"""

        for rec in self:
            products = self.env['product.product'].search([('product_tmpl_id','=',rec.product_tmpl_id.id)])
            for p in products:
                for l in p.product_template_variant_value_ids:
                    if l.product_attribute_value_id.name =='old products':

                        p.write({'vendor_id': 7791})
"""Use an approved catalogue record for an identical duplicate ERP product."""
import hashlib
import json
import re

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, ValidationError

from .submission_safety import taxpayer


def normalized(value):
    return ' '.join(re.sub(r'\s*\(qty\)', '', value or '', flags=re.I).split()).casefold()


def gtin(value):
    value = (value or '').strip()
    if len(value) not in (8, 12, 13, 14) or not value.isascii() or not value.isdigit():
        return None
    digits = [int(x) for x in value]
    if (sum(n * (3 if i % 2 == 0 else 1) for i, n in enumerate(reversed(digits[:-1])))
            + digits[-1]) % 10:
        return None
    return value.zfill(14)


class ProductCatalogue(models.Model):
    _inherit = 'product.product'

    eta_catalogue_product_id = fields.Many2one('product.product', string='ETA catalogue product',
        readonly=True, copy=False, ondelete='restrict',
        help='Reviewed identical product whose approved ETA code is used during Ahmed invoice formatting. '
             'The invoice product, SKU, stock, taxes and amounts stay unchanged.')
    eta_catalogue_identity_hash = fields.Char(readonly=True, copy=False)

    def _eta_catalogue_identity(self, target):
        self.ensure_one(); target.ensure_one()
        source = self.with_context(lang='en_US')
        target = target.with_context(lang='en_US')
        if target == source or target.eta_catalogue_product_id:
            raise ValidationError(_('ETA catalogue references cannot point to themselves or another reference.'))
        if (getattr(source, 'code_type', False) != 'egs' or getattr(target, 'code_type', False) != 'egs'
                or not getattr(target, 'e_invoicing_code', False)):
            raise ValidationError(_('The ETA catalogue reference needs an existing EGS product code.'))
        if normalized(source.name) != normalized(target.name):
            raise ValidationError(_('The ETA catalogue reference must be the same product model.'))
        identities = []
        for product in (source, target):
            attrs = {normalized(v.attribute_id.name): normalized(v.name)
                     for v in product.product_template_attribute_value_ids
                     if normalized(v.attribute_id.name) != 'vendor'}
            identifiers = [(getattr(product, key, False) or '').strip() for key in ('barcode', 'default_code')]
            identities.append({'id': product.id, 'name': product.name, 'attributes': attrs,
                'identifiers': identifiers, 'code': getattr(product, 'e_invoicing_code', False),
                'company': product.company_id.id, 'taxpayer': taxpayer(product.company_id.vat)})
        left, right = identities
        a, b = left['attributes'], right['attributes']
        if any(a[k] != b[k] for k in a.keys() & b.keys()):
            raise ValidationError(_('The ETA catalogue reference has a different product variant.'))
        shared_gtin = ({gtin(x) for x in left['identifiers']} & {gtin(x) for x in right['identifiers']}) - {None}
        shared_sku = {x.casefold() for x in left['identifiers'] if len(x) >= 5 and re.search('[a-zA-Z]', x)} & {
            x.casefold() for x in right['identifiers'] if len(x) >= 5 and re.search('[a-zA-Z]', x)}
        if not shared_gtin and not (shared_sku and a == b):
            raise ValidationError(_('The ETA catalogue reference needs the same verified GTIN or exact model/SKU and variants.'))
        if (left['company'] and right['company'] and left['taxpayer'] != right['taxpayer']):
            raise ValidationError(_('The ETA catalogue reference belongs to another taxpayer.'))
        return hashlib.sha256(json.dumps(identities, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    @api.model_create_multi
    def create(self, vals_list):
        if any(v.get('eta_catalogue_product_id') or 'eta_catalogue_identity_hash' in v for v in vals_list):
            raise ValidationError(_('Review the ETA catalogue reference after creating the product.'))
        return super().create(vals_list)

    def write(self, vals):
        if 'eta_catalogue_identity_hash' in vals:
            raise AccessError(_('The reviewed ETA catalogue identity cannot be set directly.'))
        if 'eta_catalogue_product_id' not in vals:
            return super().write(vals)
        if not self.env.su and not self.env.user.has_group('account.group_account_manager'):
            raise AccessError(_('An accounting manager must review the ETA catalogue reference.'))
        if set(vals) != {'eta_catalogue_product_id'}:
            raise ValidationError(_('Set the ETA catalogue reference separately from other product changes.'))
        target = self.sudo().with_context(active_test=False).browse(vals['eta_catalogue_product_id']).exists()
        if vals['eta_catalogue_product_id'] and not target:
            raise ValidationError(_('The reviewed ETA catalogue product no longer exists.'))
        for product in self:
            digest = product._eta_catalogue_identity(target) if target else False
            super(ProductCatalogue, product).write(dict(vals, eta_catalogue_identity_hash=digest))
        return True

    def _eta_catalogue_source_product(self, company):
        self.ensure_one()
        target = self.sudo().eta_catalogue_product_id
        if not target:
            return self
        if self._eta_catalogue_identity(target) != self.eta_catalogue_identity_hash:
            raise ValidationError(_('The ETA catalogue product identity changed; accounting must review its reference again.'))
        if target.company_id and taxpayer(target.company_id.vat) != taxpayer(company.vat):
            raise ValidationError(_('The ETA catalogue reference belongs to another invoice taxpayer.'))
        return target


class AccountMoveCatalogue(models.Model):
    _inherit = 'account.move'

    def _eta_item_code(self, product):
        return super()._eta_item_code(product._eta_catalogue_source_product(self.company_id))

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user

from ..models.product_catalogue import gtin


@tagged('post_install', '-at_install')
class TestProductCatalogue(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.ref('base.EGP').active = True
        cls.company = cls.env.company
        cls.receivable = cls.env['account.account'].create({'name': 'Catalogue reference receivable',
            'code': 'CREFAR', 'account_type': 'asset_receivable', 'reconcile': True,
            'company_ids': [Command.set(cls.company.ids)]})
        cls.income = cls.env['account.account'].create({'name': 'Catalogue reference income',
            'code': 'CREFINC', 'account_type': 'income', 'company_ids': [Command.set(cls.company.ids)]})
        cls.partner = cls.env['res.partner'].create({'name': 'Catalogue reference buyer',
            'property_account_receivable_id': cls.receivable.id})
        cls.journal = cls.env['account.journal'].create({'name': 'Catalogue reference sales', 'code': 'CREF',
            'type': 'sale', 'edi_format_ids': [Command.clear()]})
        cls.uom = cls.env.ref('uom.product_uom_unit')
        cls.uom.eta_uom_id = cls.env['eta.uom'].search([('code', '=', 'D41')], limit=1) or cls.env[
            'eta.uom'].create({'name': 'Each', 'code': 'D41'})

    def setUp(self):
        super().setUp()
        guard = patch('requests.sessions.Session.request', side_effect=AssertionError('Unexpected external HTTP'))
        guard.start(); self.addCleanup(guard.stop)
        self.codes = {}
        product_type = type(self.env['product.product'])
        # The minimal QA database does not install Tradeline's product-code add-on.
        if 'e_invoicing_code' not in self.env['product.product']._fields:
            metadata = patch.object(product_type, 'e_invoicing_code', property(lambda p: self.codes.get(p.id, False)), create=True)
            metadata.start(); self.addCleanup(metadata.stop)
        if 'code_type' not in self.env['product.product']._fields:
            kind = patch.object(product_type, 'code_type', 'egs', create=True)
            kind.start(); self.addCleanup(kind.stop)
        self.source = self.product('Phone model (qty)', '195951406107', 'SKU-PHONE-Q', 'OLD-CODE')
        self.target = self.product('Phone model', '195951406107', 'SKU-PHONE-A', 'APPROVED-CODE')

    def code(self, product, value):
        if 'e_invoicing_code' in product._fields:
            product.e_invoicing_code = value
        else:
            self.codes[product.id] = value
        if 'code_type' in product._fields:
            product.code_type = 'egs'

    def product(self, name, default, barcode, code):
        product = self.env['product.product'].create({'name': name, 'default_code': default,
            'barcode': barcode, 'uom_id': self.uom.id, 'uom_po_id': self.uom.id})
        self.code(product, code)
        return product

    def invoice(self):
        move = self.env['account.move'].create({'move_type': 'out_invoice', 'journal_id': self.journal.id,
            'partner_id': self.partner.id, 'currency_id': self.env.ref('base.EGP').id,
            'invoice_date': fields.Date.today(),
            'invoice_line_ids': [Command.create({'product_id': self.source.id, 'account_id': self.income.id,
                'quantity': 2, 'price_unit': 100, 'discount': 7, 'tax_ids': [Command.clear()]})]})
        move.action_post()
        return move

    def financial(self, move):
        return (move.state, move.date, move.invoice_date, move.amount_total, move.amount_tax,
            move.amount_untaxed, move.amount_residual, move.payment_state,
            [(l.id, l.product_id.id, l.account_id.id, l.balance, l.debit, l.credit, l.amount_currency,
              l.quantity, l.price_unit, l.discount, l.tax_ids.ids) for l in move.line_ids])

    def test_same_gtin_reference_changes_only_exported_item_code(self):
        move = self.invoice()
        before = deepcopy(self.financial(move))
        lines_before = move._get_eta_invoice_lines()
        self.source.write({'eta_catalogue_product_id': self.target.id})
        lines_after = move._get_eta_invoice_lines()
        expected = deepcopy(lines_before)
        expected[0][0]['itemCode'] = 'APPROVED-CODE'
        self.assertEqual(lines_after, expected)
        self.assertEqual(self.financial(move), before)
        self.assertEqual(self.source.e_invoicing_code, 'OLD-CODE')
        self.assertEqual(lines_after[0][0]['internalCode'], 'SKU-PHONE-Q')

    def test_missing_original_code_can_use_reviewed_identical_product(self):
        self.code(self.source, False)
        self.source.write({'eta_catalogue_product_id': self.target.id})
        self.assertEqual(self.invoice()._get_eta_invoice_lines()[0][0]['itemCode'], 'APPROVED-CODE')

    def test_no_reference_preserves_existing_formatting(self):
        self.assertEqual(self.invoice()._eta_item_code(self.source), 'OLD-CODE')

    def test_different_model_is_rejected_even_with_same_gtin(self):
        self.target.name = 'Different model'
        with self.assertRaisesRegex(ValidationError, 'same product model'):
            self.source.write({'eta_catalogue_product_id': self.target.id})

    def test_unrelated_identifiers_are_rejected(self):
        self.target.default_code = '195951406282'
        with self.assertRaisesRegex(ValidationError, 'same verified GTIN'):
            self.source.write({'eta_catalogue_product_id': self.target.id})

    def test_incorrect_gtin_check_digit_is_not_identity_evidence(self):
        self.source.default_code = self.target.default_code = '195951406108'
        self.assertIsNone(gtin('195951406108'))
        with self.assertRaisesRegex(ValidationError, 'same verified GTIN'):
            self.source.write({'eta_catalogue_product_id': self.target.id})

    def test_same_exact_manufacturer_sku_is_supported(self):
        self.source.default_code = self.target.default_code = 'A2656L11'
        self.source.write({'eta_catalogue_product_id': self.target.id})
        self.assertEqual(self.source._eta_catalogue_source_product(self.company), self.target)

    def test_variant_conflict_is_rejected_even_with_shared_gtin(self):
        color = self.env['product.attribute'].create({'name': 'Colour'})
        values = self.env['product.attribute.value'].create([
            {'name': 'Blue', 'attribute_id': color.id}, {'name': 'Red', 'attribute_id': color.id}])
        templates = self.env['product.template'].create([{'name': 'Variant model',
            'attribute_line_ids': [Command.create({'attribute_id': color.id,
                'value_ids': [Command.set(value.ids)]})]} for value in values])
        self.source, self.target = templates.mapped('product_variant_id')
        self.source.default_code = self.target.default_code = '195951406107'
        self.code(self.source, 'OLD-CODE'); self.code(self.target, 'APPROVED-CODE')
        self.assertTrue(self.source.product_template_attribute_value_ids)
        self.assertTrue(self.target.product_template_attribute_value_ids)
        with self.assertRaisesRegex(ValidationError, 'different product variant'):
            self.source.write({'eta_catalogue_product_id': self.target.id})

    def test_reference_chains_and_self_references_are_rejected(self):
        with self.assertRaisesRegex(ValidationError, 'themselves'):
            self.source.write({'eta_catalogue_product_id': self.source.id})
        self.source.write({'eta_catalogue_product_id': self.target.id})
        with self.assertRaisesRegex(ValidationError, 'another reference'):
            self.target.write({'eta_catalogue_product_id': self.source.id})

    def test_changed_target_code_stops_export_until_reviewed(self):
        self.source.write({'eta_catalogue_product_id': self.target.id})
        self.code(self.target, 'UNREVIEWED-CODE')
        with self.assertRaisesRegex(ValidationError, 'identity changed'):
            self.invoice()._eta_item_code(self.source)

    def test_reference_cannot_cross_invoice_taxpayer(self):
        self.target.company_id = self.company
        self.source.write({'eta_catalogue_product_id': self.target.id})
        with self.assertRaisesRegex(ValidationError, 'another invoice taxpayer'):
            self.source._eta_catalogue_source_product(SimpleNamespace(vat='998877665'))

    def test_invoice_staff_cannot_configure_or_forge_reference(self):
        staff = new_test_user(self.env, login='catalogue_reference_invoice_staff', groups='account.group_account_invoice')
        with self.assertRaises(AccessError):
            self.source.with_user(staff).write({'eta_catalogue_product_id': self.target.id})
        with self.assertRaises(AccessError):
            self.source.write({'eta_catalogue_identity_hash': 'forged'})

    def test_clear_reference_restores_original_code(self):
        self.source.write({'eta_catalogue_product_id': self.target.id})
        self.source.write({'eta_catalogue_product_id': False})
        self.assertFalse(self.source.eta_catalogue_identity_hash)
        self.assertEqual(self.invoice()._eta_item_code(self.source), 'OLD-CODE')

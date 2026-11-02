from lxml import etree

from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user


@tagged('post_install', '-at_install')
class TestProductEtaCodeView(TransactionCase):
    def test_existing_product_code_role_sees_original_readonly_variant_code(self):
        user = new_test_user(self.env, login='eta_product_code_editor',
            groups='base.group_user,product.group_product_manager,tradeline_product_code.group_e_invoicing_data')
        arch = etree.fromstring(self.env['product.product'].with_user(user).get_view(view_type='form')['arch'])
        fields = arch.xpath("//page[@name='E-Invoicing Data']//field[@name='e_invoicing_code']")
        self.assertEqual(len(fields), 1)
        self.assertTrue(self.env['product.product']._fields['e_invoicing_code'].readonly)
        self.assertNotIn(fields[0].get('readonly'), ('0', 'False', 'false'))
        self.assertNotIn('reasonly', fields[0].attrib)

    def test_other_product_users_do_not_gain_e_invoicing_data_access(self):
        user = new_test_user(self.env, login='eta_product_code_without_role',
            groups='base.group_user,product.group_product_manager')
        arch = etree.fromstring(self.env['product.product'].with_user(user).get_view(view_type='form')['arch'])
        self.assertFalse(arch.xpath("//page[@name='E-Invoicing Data']"))

from lxml import etree

from odoo.tests import TransactionCase, new_test_user, tagged


@tagged('post_install', '-at_install')
class TestServiceRequestScrapWizard(TransactionCase):
    def test_service_form_and_legacy_model_coexist(self):
        user = new_test_user(
            self.env, login='service_scrap_form_test', groups='stock.group_stock_user'
        )
        model = self.env['service.request.scrap.wizard'].with_user(user)
        action = self.env.ref('service_scrap_control.action_request_scrap_wizard')
        self.assertEqual(action.res_model, model._name)
        result = model.get_views([(action.view_id.id, 'form')], {})
        arch = etree.fromstring(result['views']['form']['arch'])
        fields = result['models'][model._name]['fields']
        for node in arch.xpath('//field'):
            self.assertIn(node.get('name'), fields)
        self.assertIn('picking_id', fields)
        self.assertTrue(callable(model.action_request_scrap))
        if 'request.scrap.wizard' in self.env:
            legacy = self.env['request.scrap.wizard']
            self.assertNotEqual(legacy._name, model._name)
            self.assertTrue(callable(legacy.request_scrap))

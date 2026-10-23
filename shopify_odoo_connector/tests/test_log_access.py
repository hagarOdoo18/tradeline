"""Log access must remain complete without loading every ID into a worker."""
from contextlib import contextmanager
from unittest.mock import patch

from odoo import Command
from odoo.tests.common import TransactionCase, new_test_user, tagged


@tagged('post_install', '-at_install')
class TestShopifyLogAccess(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.instances = cls.env['shopify.configuration'].create([
            {'name': 'Log QA %s' % n, 'con_endpoint': 'qa-client',
             'consumer_secret': 'qa-secret', 'shop_name': 'qa-%s.myshopify.com' % n,
             'version': '2025-10', 'company_id': cls.env.company.id}
            for n in range(3)
        ])
        cls.logs = cls.env['log.message'].create([
            {'name': 'Visible QA log %s' % n, 'model': 'product.product',
             'shopify_instance_id': cls.instances[0].id}
            for n in range(120)
        ] + [{'name': 'Hidden QA log', 'model': 'stock.quant',
              'shopify_instance_id': cls.instances[1].id}])
        cls.staff = new_test_user(
            cls.env, login='shopify_log_qa',
            groups='base.group_user,sales_team.group_sale_salesman,stock.group_stock_user,shopify_odoo_connector.group_shopify_manager')

    @contextmanager
    def forbid_log_id_materialization(self):
        field = self.instances._fields['log_message_ids']
        original = type(field).__get__

        def checked(descriptor, records, owner=None):
            if descriptor is field and records is not None:
                raise AssertionError('Log access materialized the full One2many')
            return original(descriptor, records, owner)

        with patch.object(type(field), '__get__', checked):
            yield

    def test_exact_counts_for_multiple_and_empty_instances(self):
        with self.forbid_log_id_materialization():
            self.instances._compute_customer_count()
        self.assertEqual(self.instances.mapped('log_message_count'), [120, 1, 0])

    def test_log_action_has_complete_small_domain(self):
        instance = self.instances[0].with_context(qa_context='preserved')
        with self.forbid_log_id_materialization():
            action = instance.shopify_log_message()
        self.assertEqual(action['domain'], [('shopify_instance_id', '=', instance.id)])
        self.assertEqual(action['context']['qa_context'], 'preserved')
        self.assertFalse(action['context']['create'])
        self.assertNotIn('limit', action)
        self.assertEqual(self.env['log.message'].search(action['domain']), self.logs[:120])

    def test_empty_log_action_keeps_close_behavior(self):
        with self.forbid_log_id_materialization():
            self.assertEqual(self.instances[2].shopify_log_message(),
                             {'type': 'ir.actions.act_window_close'})

    def test_counts_and_action_obey_staff_record_rules(self):
        self.env['ir.rule'].create({
            'name': 'QA hide selected log',
            'model_id': self.env['ir.model']._get_id('log.message'),
            'domain_force': "[('name', 'not ilike', 'Hidden QA log')]",
        })
        instances = self.instances.with_user(self.staff)
        with self.forbid_log_id_materialization():
            instances._compute_customer_count()
            action = instances[0].shopify_log_message()
            hidden = instances[1].shopify_log_message()
        self.assertEqual(instances.mapped('log_message_count'), [120, 0, 0])
        self.assertEqual(hidden, {'type': 'ir.actions.act_window_close'})
        self.assertEqual(self.env['log.message'].with_user(self.staff).search_count(action['domain']), 120)

    def test_other_counters_keep_native_search_domains(self):
        instance = self.instances[0]
        partner = self.env['res.partner'].create({'name': 'Shopify count QA'})
        product = self.env['product.template'].create({'name': 'Shopify count QA gift', 'gift_card': True})
        order_values = {'partner_id': partner.id}
        if 'reference_number' in self.env['sale.order']._fields:
            order_values['reference_number'] = 'SHOPIFY-COUNT-QA'
        if 'sales_rep_id' in self.env['sale.order']._fields:
            order_values['sales_rep_id'] = self.env['sales.rep'].create({
                'name': 'Shopify count QA', 'company_id': self.env.company.id}).id
        if 'branch_id' in self.env['sale.order']._fields:
            branch = self.env['res.branch'].create({
                'name': 'Shopify count QA', 'company_id': self.env.company.id})
            order_values['branch_id'] = branch.id
        order = self.env['sale.order'].create(order_values)
        self.env['shopify.sync'].create([
            {'instance_id': instance.id, 'customer_id': partner.id,
             'product_id': product.id, 'order_id': order.id},
            {'instance_id': instance.id, 'product_id': product.id},
        ])
        self.env['shopify.collection'].create({
            'name': 'Shopify count QA', 'collection': 'qa-count',
            'shopify_instance_id': instance.id})
        instance._compute_customer_count()
        for name, model, domain in [
            ('customer_count', 'res.partner', [('shopify_sync_ids.instance_id', '=', instance.id)]),
            ('product_count', 'product.template', [('shopify_sync_ids.instance_id', '=', instance.id)]),
            ('order_count', 'sale.order', [('shopify_sync_ids.instance_id', '=', instance.id)]),
            ('collection_count', 'shopify.collection', [('shopify_instance_id', '=', instance.id)]),
            ('gift_card_count', 'product.template', [('shopify_sync_ids.instance_id', '=', instance.id), ('gift_card', '=', True)]),
        ]:
            self.assertEqual(instance[name], self.env[model].search_count(domain))
            self.assertEqual(instance[name], 1)

    def test_counters_share_one_compute_and_legacy_entry_still_works(self):
        names = ['customer_count', 'product_count', 'order_count',
                 'log_message_count', 'collection_count', 'gift_card_count']
        self.assertEqual({self.instances._fields[n].compute for n in names}, {'_compute_customer_count'})
        with self.forbid_log_id_materialization():
            self.instances._compute_gift_card_count()
        self.assertEqual(self.instances[0].log_message_count, 120)

    def test_new_logs_are_not_truncated_or_stale(self):
        instance = self.instances[2]
        self.assertEqual(instance.shopify_log_message()['type'], 'ir.actions.act_window_close')
        log = self.env['log.message'].create({
            'name': 'New import diagnostic', 'model': 'stock.quant',
            'shopify_instance_id': instance.id})
        instance._compute_customer_count()
        self.assertEqual(instance.log_message_count, 1)
        self.assertEqual(self.env['log.message'].search(instance.shopify_log_message()['domain']), log)

    def test_unsaved_configuration_keeps_in_memory_log_count(self):
        instance = self.env['shopify.configuration'].new({
            'name': 'Unsaved QA', 'con_endpoint': 'qa-client',
            'consumer_secret': 'qa-secret', 'shop_name': 'new-qa.myshopify.com',
            'version': '2025-10', 'company_id': self.env.company.id,
            'log_message_ids': [Command.create({'name': 'Unsaved log', 'model': 'stock.quant'})],
        })
        instance._compute_customer_count()
        self.assertEqual(instance.log_message_count, 1)

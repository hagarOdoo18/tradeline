# -*- coding: utf-8 -*-

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestShopifyOrderEvent(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.instance = cls.env['shopify.configuration'].create({
            'name': 'API test',
            'con_endpoint': 'client-id',
            'consumer_secret': 'client-secret',
            'shop_name': 'Example-Store.MyShopify.com',
            'version': '2025-10',
            'company_id': cls.env.company.id,
            'state': 'sync',
            'order_api_key': 'test-secret',
        })
        cls.event_model = cls.env['shopify.order.event']

    def test_domain_and_gid_normalisation(self):
        self.assertEqual(
            self.event_model._canonical_domain(
                'https://EXAMPLE-store.myshopify.com/'),
            'example-store.myshopify.com')
        self.assertEqual(
            self.event_model._numeric_id(
                'gid://shopify/Order/123456789'),
            '123456789')
        self.assertEqual(
            self.event_model.find_instance(
                'example-store.myshopify.com'),
            self.instance)

    def test_derived_event_id_is_stable_per_order(self):
        first = self.event_model._event_identity(
            self.instance, 'order.created', '42', '')
        retry = self.event_model._event_identity(
            self.instance, 'order.created', '42', '')
        different = self.event_model._event_identity(
            self.instance, 'order.created', '43', '')
        self.assertEqual(first, retry)
        self.assertNotEqual(first, different)

    def test_flow_payload_is_normalised_without_product_creation(self):
        product = self.env['product.product'].create({
            'name': 'Mapped Shopify product',
            'barcode': 'SKU-42',
        })
        self.env['shopify.sync'].create({
            'instance_id': self.instance.id,
            'product_prod_id': product.id,
            'shopify_variant_id': '987',
        })
        event = self.event_model.create({
            'event_id': 'test-event',
            'topic': 'order.created',
            'instance_id': self.instance.id,
            'shopify_order_id': '123',
            'payload': {},
        })
        order = event._normalise_payload({
            'id': 'gid://shopify/Order/123',
            'name': '#1107',
            'customer': None,
            'line_items': [{
                'id': 'gid://shopify/LineItem/456',
                'variant_id': 'gid://shopify/ProductVariant/987',
                'sku': 'SKU-42',
                'title': 'Mapped Shopify product',
                'quantity': 1,
                'price': '100.00',
            }],
        }, '')
        self.assertEqual(order['id'], '123')
        self.assertEqual(order['line_items'][0]['variant_id'], '987')
        self.assertEqual(order['line_items'][0]['sku'], 'SKU-42')
        self.assertEqual(order['customer']['id'], 'guest-123')

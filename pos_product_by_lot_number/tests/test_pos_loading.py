from unittest.mock import patch

from odoo.tests.common import TransactionCase, new_test_user, tagged

from ..models import models as lot_module


@tagged('post_install', '-at_install')
class TestPosLotLoading(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Lot = cls.env['stock.lot']
        cls.Quant = cls.env['stock.quant']
        cls.product = cls.env['product.product'].create({
            'name': 'POS loading test', 'is_storable': True, 'tracking': 'lot',
        })
        # The negative-quantity case uses only this synthetic product/location.
        # Honour installations with OCA stock_no_negative instead of disabling
        # the stock check globally in the hosted transaction-test runner.
        if 'allow_negative_stock' in cls.product._fields:
            cls.product.allow_negative_stock = True
        cls.lots = cls.Lot.create([
            {'name': name, 'product_id': cls.product.id, 'company_id': cls.env.company.id}
            for name in ['POS-ZERO', 'POS-AVAILABLE', 'POS-NEGATIVE', 'POS-HISTORICAL']
        ])
        location_values = [
            ('internal', cls.env.company.id),
            ('transit', cls.env.company.id),
            ('transit', False),
            ('customer', False),
            ('supplier', False),
            ('inventory', cls.env.company.id),
        ]
        cls.locations = cls.env['stock.location'].create([
            {'name': 'POS payload %s %s' % (usage, index), 'usage': usage,
             'company_id': company_id}
            for index, (usage, company_id) in enumerate(location_values)
        ])
        cls.Quant.create([
            {'product_id': cls.product.id, 'lot_id': cls.lots[1].id,
             'location_id': location.id, 'quantity': quantity}
            for location, quantity in zip(cls.locations, [5, 2, 100, 100, 100, 100])
        ])
        cls.Quant.create({
            'product_id': cls.product.id, 'lot_id': cls.lots[2].id,
            'location_id': cls.locations[0].id, 'quantity': -2,
        })
        cls.Quant.create({
            'product_id': cls.product.id, 'lot_id': cls.lots[3].id,
            'location_id': cls.locations[3].id, 'quantity': 1,
        })
        cls.data = {'pos.config': {'data': [{'id': 0}]}}

    def _compare_payload(self, Lot=None):
        Lot = Lot if Lot is not None else self.Lot
        domain = [('id', 'in', self.lots.ids)]
        field_names = Lot._load_pos_data_fields(0)
        Lot.env.invalidate_all()
        expected = Lot.search_read(domain, field_names, load=False)
        Lot.env.invalidate_all()
        with patch.object(type(Lot), '_load_pos_data_domain', return_value=domain):
            actual = Lot._load_pos_data(self.data)
        self.assertEqual(actual['fields'], field_names)
        self.assertEqual(actual['data'], expected)
        return actual['data']

    def test_payload_matches_native_compute_and_keeps_historical_serials(self):
        rows = self._compare_payload()
        self.assertEqual({r['id']: r['product_qty'] for r in rows}, {
            self.lots[0].id: 0.0, self.lots[1].id: 7.0,
            self.lots[2].id: -2.0, self.lots[3].id: 0.0,
        })

    def test_reserved_stock_does_not_change_on_hand_quantity(self):
        quant = self.Quant.search([
            ('lot_id', '=', self.lots[1].id), ('location_id', '=', self.locations[0].id),
        ], limit=1)
        quant.reserved_quantity = 4
        self._compare_payload()

    def test_quantity_batches_keep_all_rows_and_original_order(self):
        with patch.object(lot_module, 'LOT_QUANTITY_BATCH_SIZE', 2):
            self._compare_payload()

    def test_user_company_and_quant_record_rules_match_native_compute(self):
        user = new_test_user(self.env(context=dict(self.env.context, no_reset_password=True)), login='pos_payload_cashier',
                             groups='stock.group_stock_user,point_of_sale.group_pos_user')
        self.env['ir.rule'].create({
            'name': 'POS payload quant test restriction',
            'model_id': self.env['ir.model']._get_id('stock.quant'),
            'domain_force': "[('location_id', '=', %s)]" % self.locations[0].id,
        })
        self._compare_payload(self.Lot.with_user(user))

    def test_false_domain_and_additional_fields_are_preserved(self):
        with patch.object(type(self.Lot), '_load_pos_data_domain', return_value=False):
            self.assertEqual(self.Lot._load_pos_data(self.data)['data'], [])
        field_names = self.Lot._load_pos_data_fields(0) + ['company_id']
        with patch.object(type(self.Lot), '_load_pos_data_fields', return_value=field_names):
            self._compare_payload()

    def test_loader_without_quantity_field_remains_compatible(self):
        with patch.object(type(self.Lot), '_load_pos_data_fields', return_value=['id', 'name']):
            self._compare_payload()

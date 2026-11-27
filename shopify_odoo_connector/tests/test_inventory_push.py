import json
from types import SimpleNamespace
from unittest.mock import Mock

from odoo.tests.common import BaseCase, tagged

from ..wizard.sync_inventory import GRAPHQL_MAX_RETRIES, SyncInventory


@tagged('post_install', '-at_install')
class TestInventoryPush(BaseCase):

    def setUp(self):
        super().setUp()
        self.session = Mock()
        self.log = Mock()
        self.rule = Mock()
        self.rule._apply.return_value = 4
        self.wizard = Mock(spec=SyncInventory)
        self.wizard.shopify_instance_id = SimpleNamespace(
            id=1, shop_name='test.myshopify.com',
            company_id=SimpleNamespace(id=1),
            _get_shopify_headers=lambda: {})
        self.wizard.env = {
            'log.message': SimpleNamespace(sudo=lambda: self.log),
            'shopify.min.qty': SimpleNamespace(sudo=lambda: self.rule),
        }
        self.wizard._inventory_api_version.return_value = '2025-10'
        self.wizard._supports_graphql_inventory.return_value = True
        self.wizard._get_categ_by_product.return_value = {}
        self.wizard._get_group_min_qty.return_value = (1, 'threshold')
        self.wizard._get_group_qty.return_value = 4
        self.wizard._shopify_session.return_value = self.session
        self.wizard._unstocked_inventory_indexes.side_effect = (
            SyncInventory._unstocked_inventory_indexes)
        self.wizard._post_inventory_graphql.side_effect = (
            lambda *args: SyncInventory._post_inventory_graphql(self.wizard, *args))
        self.wizard._graphql_set_levels.side_effect = (
            lambda *args: SyncInventory._graphql_set_levels(self.wizard, *args))
        self.wizard._rest_set_levels.side_effect = (
            lambda *args: SyncInventory._rest_set_levels(self.wizard, *args))
        self.location = SimpleNamespace(
            shopify_location_id='10',
            warehouse_id=SimpleNamespace(name='Shop', lot_stock_id=Mock()))
        self.groups = [{'key': 'SKU', 'product_ids': [1],
                        'variant_ids': ['1', '2', '3'],
                        'items': {'1': '101', '2': '102', '3': '103'}}]

    @staticmethod
    def _graphql_response(*errors):
        return Mock(status_code=200, **{
            'json.return_value': {
                'data': {'inventorySetQuantities': {'userErrors': list(errors)}},
            },
        })

    def _push(self, responses, pushed, failed, failure_detail=None):
        self.session.post.side_effect = responses
        result = SyncInventory._push_inventory_groups(
            self.wizard, self.groups, [self.location])
        self.assertEqual(result, dict(pushed=pushed, failed=failed, skipped=0,
                                      retried=0))
        messages = [item['name'] for item in self.log.create.call_args.args[0]]
        self.assertIn(f'{pushed} level(s) updated', messages[0])
        self.assertIn(f', {failed} failed', messages[0])
        failure_logs = [msg for msg in messages
                        if msg.startswith('Inventory push failures:')]
        if failure_detail:
            self.assertEqual(len(failure_logs), 1)
            self.assertIn(failure_detail, failure_logs[0])
        else:
            self.assertEqual(failure_logs, [])

    def test_unstocked_item_uses_rest_and_rest_of_chunk_stays_bulk(self):
        self._push([
            self._graphql_response({
                'code': 'ITEM_NOT_STOCKED_AT_LOCATION',
                'field': ['input', 'quantities', '1', 'locationId'],
            }),
            self._graphql_response(),
            Mock(status_code=200, raw=None),
        ], pushed=3, failed=0)
        first, second, fallback = self.session.post.call_args_list
        self.assertIn('code field message', json.loads(first.kwargs['data'])['query'])
        quantities = json.loads(second.kwargs['data'])['variables']['input']['quantities']
        self.assertEqual([qty['inventoryItemId'].rsplit('/', 1)[-1] for qty in quantities],
                         ['101', '103'])
        self.assertEqual(json.loads(fallback.kwargs['data'])['inventory_item_id'], '102')

    def test_unidentified_graphql_error_counts_only_failed_rest_items(self):
        self._push([
            self._graphql_response({
                'code': 'ITEM_NOT_STOCKED_AT_LOCATION',
                'field': ['input', 'quantities', '9', 'locationId'],
            }),
            Mock(status_code=200, raw=None),
            Mock(status_code=422, text='Invalid inventory item', raw=None),
            Mock(status_code=200, raw=None),
        ], pushed=2, failed=1, failure_detail='variant 2 (group SKU, location 10)')

    def test_exhausted_graphql_throttle_counts_all_deferred_levels(self):
        throttle = Mock(status_code=200, **{
            'json.return_value': {'errors': [{'extensions': {'code': 'THROTTLED'}}]},
        })
        self._push([throttle] * (GRAPHQL_MAX_RETRIES + 1),
                   pushed=0, failed=3, failure_detail='left for the next pass')
        self.assertEqual(self.session.post.call_count, GRAPHQL_MAX_RETRIES + 1)

    def test_missing_graphql_result_requires_confirmed_rest_updates(self):
        self._push([
            Mock(status_code=200, **{'json.return_value': {'data': {}}}),
            Mock(status_code=503, text='Unavailable', raw=None),
            Mock(status_code=200, raw=None),
            Mock(status_code=200, raw=None),
        ], pushed=2, failed=1, failure_detail='variant 1 (group SKU, location 10)')

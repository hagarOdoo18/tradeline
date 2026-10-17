from unittest.mock import patch

from odoo import Command
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestLastPoCost(TransactionCase):
    def test_repeated_layers_keep_latest_confirmed_price_and_fallback(self):
        products = self.env['product.product'].with_context(disable_auto_svl=True).create([
            {'name': 'Display cost PO item', 'standard_price': 70},
            {'name': 'Display cost fallback item', 'standard_price': 35}])
        partner = self.env['res.partner'].create({'name': 'Display cost supplier'})
        for state, date, price in [('purchase', '2026-01-01', 80),
                                   ('purchase', '2026-01-02', 90),
                                   ('purchase', '2026-01-02', 95),
                                   ('draft', '2026-01-03', 110)]:
            order = self.env['purchase.order'].create({
                'partner_id': partner.id, 'company_id': self.env.company.id,
                'order_line': [Command.create({'product_id': products[0].id, 'product_qty': 1,
                    'price_unit': price, 'name': 'Display cost test',
                    'product_uom': products[0].uom_po_id.id, 'date_planned': date})]})
            order.write({'state': state, 'date_approve': date})
        layers = self.env['stock.valuation.layer'].create([
            {'product_id': products[n % 2].id, 'company_id': self.env.company.id,
             'quantity': 0, 'value': 0, 'description': 'Display cost test'} for n in range(100)])
        self.env.flush_all()
        POL = type(self.env['purchase.order.line'])
        original = POL.search
        calls = []
        def counted(record, *args, **kwargs):
            calls.append(args)
            return original(record, *args, **kwargs)
        with patch.object(POL, 'search', counted):
            layers._compute_last_po_cost()
        self.assertEqual(len(calls), 2)
        for layer in layers:
            self.assertEqual(layer.last_po_cost, 95 if layer.product_id == products[0] else 35)

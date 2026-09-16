from odoo import Command
from odoo.tests.common import TransactionCase


class TestSaleOrderSro(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.sale_tax = cls.env['account.tax'].create({
            'name': 'SRO Test VAT 14%',
            'amount': 14.0,
            'amount_type': 'percent',
            'type_tax_use': 'sale',
            'company_id': cls.env.company.id,
        })

    def test_onchange_to_sro_clears_existing_line_taxes(self):
        order = self.env['sale.order'].new({
            'inv_type': 'invoice',
            'order_line': [Command.create({
                'name': 'Test line',
                'tax_id': [Command.set(self.sale_tax.ids)],
            })],
        })

        order.inv_type = 'sro'
        order._onchange_inv_type_validate_downpayment()

        self.assertFalse(order.order_line.tax_id)

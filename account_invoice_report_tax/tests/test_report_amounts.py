from unittest.mock import Mock

from odoo.tests.common import TransactionCase


class TestTaxReportAmounts(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.report = cls.env['account.invoice.report.tax.wizard']

    def test_invoice_currency_precision_and_posted_company_total(self):
        currency = Mock(round=lambda value: round(value, 3))
        invoice = Mock(
            move_type='out_invoice', currency_id=currency,
            company_currency_id=currency, tax_t1=838.821, tax_t2=0,
            tax_t2_t=0, tax_t3=0, tax_t5=0,
            amount_untaxed_in_currency_signed=5991.579,
            amount_total_in_currency_signed=6830.4,
            amount_total_signed=6830.4,
        )
        amounts = self.report._invoice_amounts(invoice)
        self.assertEqual(amounts['tax_t1'], 838.821)
        self.assertEqual(amounts['total'], 6830.4)

    def test_net_converted_uses_posted_company_currency_amount(self):
        invoice_currency = Mock(round=lambda value: round(value, 3))
        company_currency = Mock(round=lambda value: round(value, 3))
        invoice = Mock(
            move_type='out_invoice', currency_id=invoice_currency,
            company_currency_id=company_currency, tax_t1=0, tax_t2=0,
            tax_t2_t=0, tax_t3=0, tax_t5=0,
            amount_untaxed_in_currency_signed=2513.345,
            amount_total_in_currency_signed=2513.345,
            amount_total_signed=127853.859,
        )
        amounts = self.report._invoice_amounts(invoice)
        self.assertEqual(amounts['total_converted'], 127853.859)

from odoo.tests.common import TransactionCase
from unittest.mock import Mock


class TestInvoiceReportSettlementSemantics(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.report = cls.env['account.invoice.duo.wizard']

    def test_reconciliation_direction_controls_sign(self):
        self.assertEqual(self.report._signed_settlement_amount(1, 21598.4), 21598.4)
        self.assertEqual(self.report._signed_settlement_amount(-1, 72500), -72500)

    def test_pos_mixed_sign_split_is_preserved(self):
        rec = {1: [('Card', -59990, 'pos_reconciliation', 10)]}
        pos = {1: [('Card', -79990, 'pos', 10), ('Cash', 20000, 'pos', 11)]}
        self.assertEqual(self.report._combine_settlements(rec, pos)[1], pos[1])

    def test_partial_paid_original_leaves_credit_balance_due(self):
        rows = self.report._allocate_credit([('Card', 60, 'payment', 10)], 100, 0, 40)
        self.assertEqual(rows, [('Card', -24, 'attributed', 10)])

    def test_split_methods_are_proportional(self):
        methods = [('Cash', 30, 'payment', 10), ('Card', 70, 'payment', 11)]
        self.assertEqual(self.report._allocate_credit(methods, 100, 0, 50), [
            ('Cash', -15, 'attributed', 10), ('Card', -35, 'attributed', 11),
        ])

    def test_sibling_credits_cannot_exceed_original_capacity(self):
        methods = [('Card', 100, 'payment', 10)]
        remaining = {(10, 'Card'): 100}
        first = self.report._allocate_credit(methods, 100, 0, 80, remaining)
        second = self.report._allocate_credit(methods, 100, 80, 50, remaining)
        self.assertEqual(sum(-row[1] for row in first + second), 100)

    def test_negative_net_source_method_is_not_guessed(self):
        methods = [('Cash', 100, 'payment', 10), ('Change', -10, 'pos', 11)]
        self.assertEqual(self.report._allocate_credit(methods, 100, 0, 50), [])

    def test_depleted_method_is_redistributed_independent_of_order(self):
        first_order = [('A', 50, 'payment', 10), ('B', 50, 'payment', 11)]
        reverse_order = list(reversed(first_order))
        remaining_one = {(10, 'A'): 50, (11, 'B'): 0}
        remaining_two = dict(remaining_one)
        expected = [('A', -50, 'attributed', 10)]
        self.assertEqual(
            self.report._allocate_credit(first_order, 100, 50, 50, remaining_one), expected
        )
        self.assertEqual(
            self.report._allocate_credit(reverse_order, 100, 50, 50, remaining_two), expected
        )

    def test_rounding_uses_largest_remainders_without_exceeding_target(self):
        currency = Mock(rounding=0.01, round=lambda value: round(value + 1e-12, 2))
        methods = [(name, 25, 'payment', journal) for name, journal in zip('ABCD', range(4))]
        rows = self.report._allocate_credit(methods, 100, 0, 0.02, currency=currency)
        self.assertEqual(sum(-row[1] for row in rows), 0.02)
        self.assertEqual(len(rows), 2)

    def test_repeated_cents_do_not_lose_last_currency_unit(self):
        currency = Mock(rounding=0.01, round=lambda value: round(value + 1e-12, 2))
        methods = [('Cash', 0.30, 'payment', 10)]
        remaining = {(10, 'Cash'): 0.30}
        amounts = []
        for prior in (0, 0.10, 0.20):
            rows = self.report._allocate_credit(
                methods, 0.30, prior, 0.10, remaining, currency
            )
            amounts.append(-sum(row[1] for row in rows))
        self.assertEqual(amounts, [0.10, 0.10, 0.10])
        self.assertEqual(remaining[(10, 'Cash')], 0.0)

    def test_on_screen_summary_keeps_currency_totals_separate(self):
        currencies = self.env['res.currency'].search([], limit=2)
        self.assertEqual(len(currencies), 2)
        rows = []
        for index, currency in enumerate(currencies, start=1):
            rows.append({
                'currency': currency,
                'payment_amount': index * 10,
                'show_invoice': True,
                'total_net': index * 100,
                'report_amount_due': index * 90,
                'accounting_amount_due': index * 80,
            })
        summary = self.report._report_currency_summary(rows)
        for currency in currencies:
            self.assertEqual(summary.count(currency.name), 1)
        self.assertEqual(len(summary.splitlines()), 2)

    def test_tax_columns_use_invoice_currency_precision(self):
        invoice = Mock(
            currency_id=Mock(round=lambda value: round(value, 3)),
            tax_t1=3350.23, tax_t2=0, tax_t2_t=0, tax_t3=0, tax_t5=0,
            amount_untaxed_in_currency_signed=23930.17,
        )
        amounts = self.report._invoice_tax_values(invoice, 1)
        self.assertEqual(amounts['tax_14'], 3350.23)
        self.assertEqual(amounts['subtotal_with_tax_14'], 27280.4)

        invoice.tax_t1 = 838.821
        invoice.amount_untaxed_in_currency_signed = 5991.579
        amounts = self.report._invoice_tax_values(invoice, 1)
        self.assertEqual(amounts['tax_14'], 838.821)
        self.assertEqual(amounts['subtotal_with_tax_14'], 6830.4)

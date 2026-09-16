from odoo.tests.common import TransactionCase


class TestAccountMoveTaxAmounts(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.move_model = cls.env['account.move']

    def test_customer_invoice_tax_line_uses_posted_amount(self):
        self.assertEqual(
            self.move_model._document_tax_amount(-1, -3350.23), 3350.23
        )
        self.assertEqual(
            self.move_model._document_tax_amount(-1, -838.821), 838.821
        )

    def test_customer_refund_tax_line_uses_posted_amount(self):
        self.assertEqual(
            self.move_model._document_tax_amount(1, 622.877), 622.877
        )

    def test_withholding_tax_keeps_negative_bucket_value(self):
        self.assertEqual(
            self.move_model._document_tax_amount(-1, 307.009), -307.009
        )

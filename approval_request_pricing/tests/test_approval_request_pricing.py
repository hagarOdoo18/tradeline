from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestApprovalRequestPricing(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.category = cls.env["approval.category"].create(
            {
                "name": "RFQ Pricing Test",
                "company_id": cls.env.company.id,
            }
        )
        cls.request = cls.env["approval.request"].create(
            {
                "name": "RFQ Pricing Test",
                "category_id": cls.category.id,
                "request_owner_id": cls.env.user.id,
            }
        )
        cls.product = cls.env["product.product"].create(
            {
                "name": "Approval Pricing Product",
                "standard_price": 80.0,
                "list_price": 125.0,
            }
        )

    def test_product_prices_do_not_default_from_product(self):
        line = self.env["approval.product.line"].create(
            {
                "approval_request_id": self.request.id,
                "product_id": self.product.id,
            }
        )

        self.assertEqual(line.unit_cost, 0.0)
        self.assertEqual(line.selling_price, 0.0)
        self.assertEqual(line.margin, 0.0)

    def test_product_prices_and_margin_are_manual_fields(self):
        line = self.env["approval.product.line"].create(
            {
                "approval_request_id": self.request.id,
                "product_id": self.product.id,
                "unit_cost": 90.0,
                "selling_price": 140.0,
                "margin": 35.0,
            }
        )

        self.assertEqual(line.unit_cost, 90.0)
        self.assertEqual(line.selling_price, 140.0)
        self.assertEqual(line.margin, 35.0)

    def test_payment_fields_are_standalone_text(self):
        self.request.write(
            {
                "payment_terms": "15 days after delivery",
                "method_type": "Bank transfer",
            }
        )

        self.assertEqual(self.request.payment_terms, "15 days after delivery")
        self.assertEqual(self.request.method_type, "Bank transfer")
        self.assertEqual(self.request._fields["payment_terms"].type, "char")
        self.assertEqual(self.request._fields["method_type"].type, "char")

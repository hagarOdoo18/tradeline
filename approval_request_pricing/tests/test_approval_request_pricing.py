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

    def test_product_prices_and_margin_default_from_product(self):
        line = self.env["approval.product.line"].create(
            {
                "approval_request_id": self.request.id,
                "product_id": self.product.id,
            }
        )

        self.assertEqual(line.unit_cost, 80.0)
        self.assertEqual(line.selling_price, 125.0)
        self.assertEqual(line.margin, 45.0)

    def test_margin_recomputes_after_manual_price_change(self):
        line = self.env["approval.product.line"].create(
            {
                "approval_request_id": self.request.id,
                "product_id": self.product.id,
                "unit_cost": 90.0,
                "selling_price": 140.0,
            }
        )

        self.assertEqual(line.margin, 50.0)

    def test_payment_fields_are_dropdown_relations(self):
        payment_term_field = self.request._fields["payment_term_id"]
        method_type_field = self.request._fields["payment_method_type_id"]

        self.assertEqual(payment_term_field.comodel_name, "account.payment.term")
        self.assertEqual(method_type_field.comodel_name, "account.payment.method")

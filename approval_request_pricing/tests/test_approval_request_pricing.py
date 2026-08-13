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

    def test_selling_price_is_editable_and_margin_recomputes(self):
        line = self.env["approval.product.line"].create(
            {
                "approval_request_id": self.request.id,
                "product_id": self.product.id,
                "selling_price": 140.0,
            }
        )

        self.assertEqual(line.unit_cost, 80.0)
        self.assertEqual(line.selling_price, 140.0)
        self.assertEqual(line.margin, 60.0)

    def test_unit_cost_is_editable_and_margin_recomputes(self):
        line = self.env["approval.product.line"].create(
            {
                "approval_request_id": self.request.id,
                "product_id": self.product.id,
                "unit_cost": 90.0,
            }
        )

        self.assertEqual(line.unit_cost, 90.0)
        self.assertEqual(line.selling_price, 125.0)
        self.assertEqual(line.margin, 35.0)

    def test_request_totals_include_quantity_and_recompute(self):
        line = self.env["approval.product.line"].create(
            {
                "approval_request_id": self.request.id,
                "product_id": self.product.id,
                "quantity": 3.0,
                "unit_cost": 90.0,
                "selling_price": 140.0,
            }
        )

        self.assertEqual(self.request.total_cost, 270.0)
        self.assertEqual(self.request.total_selling, 420.0)
        self.assertEqual(self.request.total_margin, 150.0)

        line.write({"unit_cost": 100.0, "selling_price": 150.0})

        self.assertEqual(self.request.total_cost, 300.0)
        self.assertEqual(self.request.total_selling, 450.0)
        self.assertEqual(self.request.total_margin, 150.0)

    def test_payment_fields_use_configurable_options(self):
        payment_term = self.env["approval.payment.term.option"].create(
            {"name": "15 days after delivery"}
        )
        method_type = self.env["approval.method.type.option"].create(
            {"name": "Bank transfer"}
        )
        self.request.write(
            {
                "payment_term_option_id": payment_term.id,
                "method_type_option_id": method_type.id,
            }
        )

        self.assertEqual(self.request.payment_term_option_id, payment_term)
        self.assertEqual(self.request.method_type_option_id, method_type)
        self.assertEqual(
            self.request._fields["payment_term_option_id"].comodel_name,
            "approval.payment.term.option",
        )
        self.assertEqual(
            self.request._fields["method_type_option_id"].comodel_name,
            "approval.method.type.option",
        )

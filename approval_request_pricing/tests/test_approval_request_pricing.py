from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user


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

    def test_custom_fields_require_approvals_group(self):
        group = self.env.ref("approval_request_pricing.group_approvals")
        self.assertEqual(group.name, "Approvals")
        self.assertEqual(
            group.category_id,
            self.env.ref("base.module_category_human_resources_approvals"),
        )
        self.assertIn(
            self.env.ref("approvals.group_approval_user"), group.implied_ids
        )
        self.assertNotIn(
            group, self.env.ref("base.group_system").implied_ids
        )
        self.assertEqual(
            self.env.ref("approvals.approvals_menu_root").groups_id,
            group,
        )

        restricted_fields = {
            "payment_term_option_id",
            "method_type_option_id",
            "pricing_currency_id",
            "total_cost",
            "total_selling",
            "total_margin",
        }
        for field_name in restricted_fields:
            self.assertEqual(
                self.request._fields[field_name].groups,
                "approval_request_pricing.group_approvals",
            )

        line_fields = {"currency_id", "unit_cost", "selling_price", "margin"}
        for field_name in line_fields:
            self.assertEqual(
                self.env["approval.product.line"]._fields[field_name].groups,
                "approval_request_pricing.group_approvals",
            )

        user_without_access = new_test_user(
            self.env,
            login="approval_pricing_hidden",
            groups="base.group_user,approvals.group_approval_user",
        )
        user_with_access = new_test_user(
            self.env,
            login="approval_pricing_visible",
            groups=(
                "base.group_user,approvals.group_approval_user,"
                "approval_request_pricing.group_approvals"
            ),
        )

        hidden_request_fields = self.env["approval.request"].with_user(
            user_without_access
        ).fields_get()
        visible_request_fields = self.env["approval.request"].with_user(
            user_with_access
        ).fields_get()
        for field_name in restricted_fields:
            self.assertNotIn(field_name, hidden_request_fields)
            self.assertIn(field_name, visible_request_fields)

        hidden_line_fields = self.env["approval.product.line"].with_user(
            user_without_access
        ).fields_get()
        visible_line_fields = self.env["approval.product.line"].with_user(
            user_with_access
        ).fields_get()
        for field_name in line_fields:
            self.assertNotIn(field_name, hidden_line_fields)
            self.assertIn(field_name, visible_line_fields)

    def test_approved_unit_cost_is_applied_to_generated_rfq_line(self):
        vendor = self.env["res.partner"].create(
            {"name": "Approval Pricing Vendor", "supplier_rank": 1}
        )
        purchase_order = self.env["purchase.order"].create(
            {"partner_id": vendor.id}
        )
        purchase_line = self.env["purchase.order.line"].create(
            {
                "order_id": purchase_order.id,
                "product_id": self.product.id,
                "product_qty": 2.0,
                "price_unit": 55.0,
            }
        )
        approval_line = self.env["approval.product.line"].create(
            {
                "approval_request_id": self.request.id,
                "product_id": self.product.id,
                "quantity": 2.0,
                "unit_cost": 91.0,
                "purchase_order_line_id": purchase_line.id,
            }
        )

        approval_line._apply_approved_unit_cost_to_purchase_order_line()

        self.assertEqual(purchase_line.price_unit, 91.0)

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase


class TestPosDiscountReasonRequired(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.product = cls.env["product.product"].create({
            "name": "Discount reason regression product",
            "list_price": 100.0,
        })
        cls.reason = cls.env["discount.reason"].create({
            "name": "Approved 50% test discount",
            "start_date": fields.Date.today(),
            "end_date": fields.Date.today(),
            "company_ids": [(6, 0, cls.env.company.ids)],
            "discount_percentage": 50.0,
        })

    def test_ui_payload_rejects_discount_without_reason(self):
        payload = {
            "data": {
                "discount_reason_id": False,
                "lines": [[0, 0, {
                    "product_id": self.product.id,
                    "qty": 1.0,
                    "price_unit": 100.0,
                    "discount": 40.0,
                }]],
            },
        }

        with self.assertRaisesRegex(UserError, "Discount Reason is mandatory"):
            self.env["pos.order"]._validate_locked_category_discounts(payload)

    def test_ui_payload_allows_zero_discount_without_reason(self):
        payload = {
            "data": {
                "discount_reason_id": False,
                "lines": [[0, 0, {
                    "product_id": self.product.id,
                    "qty": 1.0,
                    "price_unit": 100.0,
                    "discount": 0.0,
                }]],
            },
        }

        self.env["pos.order"]._validate_locked_category_discounts(payload)

    def test_ui_payload_allows_discount_with_reason(self):
        payload = {
            "data": {
                "discount_reason_id": self.reason.id,
                "lines": [[0, 0, {
                    "product_id": self.product.id,
                    "qty": 1.0,
                    "price_unit": 100.0,
                    "discount": 40.0,
                }]],
            },
        }

        self.env["pos.order"]._validate_locked_category_discounts(payload)

    def test_saved_order_constraint_rejects_discount_without_reason(self):
        order = self.env["pos.order"].new({
            "discount_reason_id": False,
            "lines": [(0, 0, {
                "product_id": self.product.id,
                "qty": 1.0,
                "price_unit": 100.0,
                "discount": 50.0,
            })],
        })

        with self.assertRaisesRegex(ValidationError, "Discount Reason is mandatory"):
            order._check_discount_reason_required_for_discount()

    def test_saved_line_constraint_rejects_discount_without_reason(self):
        order = self.env["pos.order"].new({
            "discount_reason_id": False,
            "lines": [(0, 0, {
                "product_id": self.product.id,
                "qty": 1.0,
                "price_unit": 100.0,
                "discount": 40.0,
            })],
        })

        with self.assertRaisesRegex(ValidationError, "Discount Reason is mandatory"):
            order.lines._check_discount_reason_required_for_discount()

    def test_saved_order_constraint_allows_zero_discount_without_reason(self):
        order = self.env["pos.order"].new({
            "discount_reason_id": False,
            "lines": [(0, 0, {
                "product_id": self.product.id,
                "qty": 1.0,
                "price_unit": 100.0,
                "discount": 0.0,
            })],
        })

        order._check_discount_reason_required_for_discount()

    def test_saved_order_and_line_constraints_allow_reason(self):
        order = self.env["pos.order"].new({
            "discount_reason_id": self.reason.id,
            "lines": [(0, 0, {
                "product_id": self.product.id,
                "qty": 1.0,
                "price_unit": 100.0,
                "discount": 40.0,
            })],
        })

        order._check_discount_reason_required_for_discount()
        order.lines._check_discount_reason_required_for_discount()

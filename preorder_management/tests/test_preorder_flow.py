# -*- coding: utf-8 -*-

from datetime import timedelta
from unittest import SkipTest

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged
from odoo.tools import float_compare


@tagged("post_install", "-at_install")
class TestPreorderFlow(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company

        cls.branch = cls.env["res.branch"].search(
            [("company_id", "=", cls.company.id)], order="id", limit=1
        )
        if not cls.branch:
            raise SkipTest("No branch is configured for the staging company.")

        # account.payment.search_fetch() is branch-filtered by the installed
        # branch addon. Mirror a real Branch Manager session so posted direct
        # payments remain visible to the workflow under test.
        if cls.branch not in cls.env.user.branch_ids:
            cls.env.user.sudo().write({"branch_ids": [Command.link(cls.branch.id)]})

        cls.warehouse = cls.env["stock.warehouse"].search(
            [
                ("company_id", "=", cls.company.id),
                ("branch_id", "=", cls.branch.id),
            ],
            order="id",
            limit=1,
        )
        cls.invoice_journal = cls.env["account.journal"].search(
            [
                ("company_id", "=", cls.company.id),
                ("branch_id", "=", cls.branch.id),
                ("type", "=", "sale"),
            ],
            order="id",
            limit=1,
        )
        cls.payment_journal = cls.env["account.journal"].search(
            [
                ("company_id", "=", cls.company.id),
                ("type", "in", ("bank", "cash")),
                "|",
                ("branch_id", "=", cls.branch.id),
                ("branch_id", "=", False),
            ],
            order="branch_id desc, id",
            limit=1,
        )
        cls.payment_method_line = cls.payment_journal.inbound_payment_method_line_ids[:1]
        cls.second_payment_journal = cls.env["account.journal"].search(
            [
                ("company_id", "=", cls.company.id),
                ("type", "in", ("bank", "cash")),
                ("id", "!=", cls.payment_journal.id),
                "|",
                ("branch_id", "=", cls.branch.id),
                ("branch_id", "=", False),
            ],
            order="branch_id desc, id",
            limit=1,
        )
        if not cls.second_payment_journal.inbound_payment_method_line_ids:
            cls.second_payment_journal = cls.payment_journal
        cls.second_payment_method_line = (
            cls.second_payment_journal.inbound_payment_method_line_ids[:1]
        )
        cls.sales_rep = cls.env["sales.rep"].search(
            ["|", ("branch_id", "=", cls.branch.id), ("branch_id", "=", False)],
            order="branch_id desc, id",
            limit=1,
        )
        cls.product = cls.env["product.product"].search(
            [
                ("sale_ok", "=", True),
                ("taxes_id", "!=", False),
                ("type", "!=", "service"),
            ],
            order="id",
            limit=1,
        )
        cls.pricelist = cls.env["product.pricelist"].search(
            [("company_id", "in", (False, cls.company.id))],
            order="company_id desc, id",
            limit=1,
        )
        required = {
            "warehouse": cls.warehouse,
            "invoice journal": cls.invoice_journal,
            "payment journal": cls.payment_journal,
            "inbound payment method": cls.payment_method_line,
            "sales rep": cls.sales_rep,
            "taxed saleable product": cls.product,
            "pricelist": cls.pricelist,
        }
        missing = [name for name, record in required.items() if not record]
        if missing:
            raise SkipTest("Missing staging configuration: %s" % ", ".join(missing))

        cls.customer = cls.env["res.partner"].create(
            {
                "name": "Pre-order Automated Transaction Test",
                "company_id": cls.company.id,
                "property_product_pricelist": cls.pricelist.id,
            }
        )
        today = fields.Date.today()
        cls.campaign = cls.env["sale.preorder.campaign"].sudo().create(
            {
                "name": "Automated Pre-order Rollback Test",
                "company_id": cls.company.id,
                "date_start": today,
                "date_end": today + timedelta(days=30),
                "product_ids": [Command.set(cls.product.ids)],
                "branch_ids": [Command.set(cls.branch.ids)],
                "allocation_line_ids": [
                    Command.create(
                        {
                            "branch_id": cls.branch.id,
                            "product_id": cls.product.id,
                            "allocated_qty": 5.0,
                        }
                    )
                ],
            }
        )
        cls.campaign.action_open_campaign()

    def _post_payment(self, preorder, amount=None, journal=None, payment_method_line=None):
        journal = journal or self.payment_journal
        payment_method_line = payment_method_line or journal.inbound_payment_method_line_ids[:1]
        payment = self.env["account.payment"].sudo().create(
            {
                "payment_type": "inbound",
                "partner_type": "customer",
                "partner_id": preorder.customer_id.id,
                "company_id": preorder.company_id.id,
                "amount": amount if amount is not None else preorder.deposit_amount,
                "currency_id": preorder.currency_id.id,
                "date": fields.Date.today(),
                "journal_id": journal.id,
                "payment_method_line_id": payment_method_line.id,
                "memo": preorder.name,
                "preorder_payment_id": preorder.id,
            }
        )
        payment.action_post()
        preorder.invalidate_recordset()
        return payment

    def test_payment_reservation_and_delivery_order(self):
        preorder = self.env["sale.preorder"].sudo().create(
            {
                "campaign_id": self.campaign.id,
                "customer_id": self.customer.id,
                "branch_id": self.branch.id,
                "sales_rep_id": self.sales_rep.id,
                "product_id": self.product.id,
                "requested_qty": 1.0,
            }
        )
        original_unit_price = preorder.price_unit
        original_total = preorder.deposit_amount
        self.assertGreater(original_unit_price, 0.0)
        self.assertGreater(original_total, 0.0)
        self.assertEqual(
            float_compare(
                preorder.amount_untaxed + preorder.amount_tax,
                preorder.deposit_amount,
                precision_rounding=preorder.currency_id.rounding,
            ),
            0,
        )
        self.assertFalse(preorder.source_order_id)
        self.assertEqual(preorder.state, "draft")
        self.assertFalse(preorder.allocation_id)
        allocation = self.campaign.allocation_line_ids
        allocation.invalidate_recordset(["reserved_qty", "available_qty"])
        self.assertEqual(allocation.reserved_qty, 0.0)
        self.assertEqual(allocation.available_qty, 5.0)
        self.campaign.invalidate_recordset(
            ["quota_quantity", "allocated_quantity", "available_quantity"]
        )
        self.assertEqual(self.campaign.quota_quantity, 5.0)
        self.assertEqual(self.campaign.allocated_quantity, 0.0)
        self.assertEqual(self.campaign.available_quantity, 5.0)

        preorder.write({"discount": 1.0})
        self.assertEqual(preorder.price_unit, original_unit_price)
        self.assertLess(preorder.deposit_amount, original_total)
        self.assertFalse(preorder.allocation_id)
        preorder.write({"discount": 0.0})
        self.assertEqual(preorder.price_unit, original_unit_price)
        self.assertEqual(
            float_compare(
                preorder.deposit_amount,
                original_total,
                precision_rounding=preorder.currency_id.rounding,
            ),
            0,
        )

        unpaid_preorder = self.env["sale.preorder"].sudo().create(
            {
                "campaign_id": self.campaign.id,
                "customer_id": self.customer.id,
                "branch_id": self.branch.id,
                "sales_rep_id": self.sales_rep.id,
                "product_id": self.product.id,
                "requested_qty": 1.0,
            }
        )
        self.assertFalse(unpaid_preorder.allocation_id)
        allocation.invalidate_recordset(["reserved_qty", "available_qty"])
        self.assertEqual(allocation.reserved_qty, 0.0)
        unpaid_preorder.action_confirm_preorder()
        self.assertFalse(unpaid_preorder.allocation_id)

        cancelled_preorder = self.env["sale.preorder"].sudo().create(
            {
                "campaign_id": self.campaign.id,
                "customer_id": self.customer.id,
                "branch_id": self.branch.id,
                "sales_rep_id": self.sales_rep.id,
                "product_id": self.product.id,
                "requested_qty": 1.0,
            }
        )
        self.assertFalse(cancelled_preorder.allocation_id)
        cancelled_preorder.action_cancel_preorder()
        self.assertEqual(cancelled_preorder.state, "cancelled")
        self.assertFalse(cancelled_preorder.allocation_id)
        allocation.invalidate_recordset(["reserved_qty", "available_qty"])
        self.assertEqual(allocation.reserved_qty, 0.0)

        preorder.action_confirm_preorder()
        self.assertEqual(preorder.state, "confirmed")
        payment_action = preorder.action_register_payment()
        self.assertEqual(payment_action["res_model"], "account.payment")
        self.assertEqual(
            float_compare(
                payment_action["context"]["default_amount"],
                preorder.deposit_amount,
                precision_rounding=preorder.currency_id.rounding,
            ),
            0,
        )
        self.assertEqual(
            payment_action["context"]["default_preorder_payment_id"], preorder.id
        )
        self.assertEqual(payment_action["context"]["default_memo"], preorder.name)

        first_payment_amount = preorder.currency_id.round(preorder.deposit_amount / 2.0)
        self._post_payment(preorder, amount=first_payment_amount)
        self.assertEqual(preorder.state, "confirmed")
        self.assertFalse(preorder.allocation_id)
        self.assertEqual(preorder.payment_count, 1)

        payment = self._post_payment(
            preorder,
            amount=preorder.deposit_amount - first_payment_amount,
            journal=self.second_payment_journal,
            payment_method_line=self.second_payment_method_line,
        )
        self.assertEqual(preorder.state, "pending")
        self.assertTrue(preorder.allocation_id)
        self.assertEqual(preorder.payment_count, 2)
        self.assertEqual(preorder.payment_status, "available")
        self.assertEqual(payment.branch_id, self.branch)
        self.assertIn(self.payment_journal.display_name, preorder.payment_method_breakdown)
        self.assertIn(self.second_payment_journal.display_name, preorder.payment_method_breakdown)
        expected_breakdown_rows = 2 if self.second_payment_journal != self.payment_journal else 1
        self.assertEqual(
            str(preorder.payment_method_breakdown_html).count("text-nowrap"),
            expected_breakdown_rows,
        )
        self.assertEqual(
            float_compare(
                preorder.get_report_payment_total(),
                preorder.deposit_amount,
                precision_rounding=preorder.currency_id.rounding,
            ),
            0,
        )
        report_action = self.env.ref(
            "preorder_management.action_report_preorder_confirmation"
        )
        report_html, _ = report_action._render_qweb_html(
            report_action.report_name, preorder.ids
        )
        self.assertIn(b"Reserved Device", report_html)
        self.assertIn(b"Total Paid", report_html)
        allocation.invalidate_recordset(["reserved_qty", "available_qty"])
        self.assertEqual(allocation.reserved_qty, 1.0)
        self.assertEqual(allocation.available_qty, 4.0)
        self.campaign.invalidate_recordset(
            ["quota_quantity", "allocated_quantity", "available_quantity"]
        )
        self.assertEqual(self.campaign.quota_quantity, 5.0)
        self.assertEqual(self.campaign.allocated_quantity, 1.0)
        self.assertEqual(self.campaign.available_quantity, 4.0)

        allocation.write({"allocated_qty": 1.0})
        with self.assertRaisesRegex(UserError, "No new pre-order can be created"):
            with self.env.cr.savepoint():
                self.env["sale.preorder"].sudo().create(
                    {
                        "campaign_id": self.campaign.id,
                        "customer_id": self.customer.id,
                        "branch_id": self.branch.id,
                        "sales_rep_id": self.sales_rep.id,
                        "product_id": self.product.id,
                        "requested_qty": 1.0,
                    }
                )
        allocation.write({"allocated_qty": 5.0})

        self.campaign.action_open_allocation_delivery()
        self.assertEqual(self.campaign.state, "delivery")
        self.assertEqual(preorder.state, "allocated")
        self.assertEqual(preorder.allocation_id.branch_id, self.branch)
        self.assertEqual(unpaid_preorder.state, "confirmed")
        self.assertFalse(unpaid_preorder.allocation_id)

        self._post_payment(unpaid_preorder)
        self.assertEqual(unpaid_preorder.state, "allocated")
        self.assertTrue(unpaid_preorder.allocation_id)
        allocation.invalidate_recordset(["reserved_qty", "available_qty"])
        self.assertEqual(allocation.reserved_qty, 2.0)
        self.campaign.invalidate_recordset(
            ["allocated_quantity", "available_quantity"]
        )
        self.assertEqual(self.campaign.allocated_quantity, 2.0)
        self.assertEqual(self.campaign.available_quantity, 3.0)

        delivery_values = preorder._prepare_delivery_order_values()
        self.assertEqual(delivery_values["order_line"][0][2]["price_unit"], original_unit_price)
        self.assertEqual(delivery_values["order_line"][0][2]["discount"], 0.0)
        preorder.action_create_delivery_order()
        self.assertEqual(preorder.state, "delivery")
        self.assertTrue(preorder.final_sale_order_id)
        self.assertFalse(preorder.final_sale_order_id.preorder_source_order_id)
        self.assertEqual(preorder.final_sale_order_id.preorder_id, preorder)
        self.assertEqual(len(preorder.final_sale_order_id.order_line), 1)
        self.assertEqual(
            preorder.final_sale_order_id.order_line.price_unit, original_unit_price
        )

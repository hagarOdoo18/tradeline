# -*- coding: utf-8 -*-

from datetime import timedelta
from unittest import SkipTest
from unittest.mock import Mock

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
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

    def _post_payment(
        self, preorder, amount=None, journal=None, payment_method_line=None, date=None
    ):
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
                "date": date or fields.Date.today(),
                "journal_id": journal.id,
                "payment_method_line_id": payment_method_line.id,
                "memo": preorder.name,
                "preorder_payment_id": preorder.id,
            }
        )
        payment.action_post()
        preorder.invalidate_recordset()
        return payment

    def test_migrate_preorder_payment_reverses_without_releasing_reservation(self):
        if not self.payment_journal.outbound_payment_method_line_ids:
            raise SkipTest("No outbound payment method is configured for the test journal.")
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
        preorder.action_confirm_preorder()
        original = self._post_payment(preorder)
        original_state = original.state
        self.assertIn(preorder.state, ("pending", "allocated"))
        reserved_before = preorder.allocation_id.reserved_qty

        preorder.migrate_payments_to_delivery()
        preorder.invalidate_recordset()
        original.invalidate_recordset(["state", "date", "move_id"])
        confirmation = preorder.payment_confirmation_ids.filtered(
            lambda item: item.source_payment_id == original
        )
        self.assertEqual(len(confirmation), 1)
        self.assertTrue(confirmation.reversal_payment_id)
        self.assertEqual(confirmation.amount, original.amount)
        self.assertEqual(preorder.payment_recording_mode, "delivery")
        self.assertEqual(original.state, original_state)
        self.assertEqual(preorder._get_delivery_payment_confirmed_amount(), preorder.deposit_amount)
        preorder.allocation_id.invalidate_recordset(["reserved_qty"])
        self.assertEqual(preorder.allocation_id.reserved_qty, reserved_before)

    def test_guarded_payment_redate_works_without_general_unreconcile_access(self):
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
        preorder.action_confirm_preorder()
        original_date = fields.Date.today() - timedelta(days=1)
        payment = self._post_payment(preorder, date=original_date)
        original_name = payment.name
        original_amount = payment.amount
        original_journal = payment.journal_id

        workflow_groups = (
            self.env.ref("base.group_user")
            | self.env.ref("account.group_account_invoice")
            | self.env.ref("point_of_sale.group_pos_user")
            | self.env.ref("branch.group_branch_user")
            | self.env.ref("preorder_management.group_preorder_user")
        )
        cashier = self.env["res.users"].with_context(
            no_reset_password=True
        ).sudo().create(
            {
                "name": "Automated POS Pre-order Cashier",
                "login": "automated_pos_preorder_cashier",
                "email": "automated_pos_preorder_cashier@example.com",
                "company_id": self.company.id,
                "company_ids": [Command.set(self.company.ids)],
                "branch_id": self.branch.id,
                "branch_ids": [Command.set(self.branch.ids)],
                "groups_id": [Command.set(workflow_groups.ids)],
            }
        )
        self.assertFalse(cashier.has_group("branch.group_unreconcile"))

        # The cashier still cannot reset arbitrary payments from the normal UI.
        with self.assertRaisesRegex(AccessError, "not allowed to unreconcile"):
            with self.env.cr.savepoint():
                payment.with_user(cashier).action_draft()

        invoice = self.env["account.move"].with_context(
            branch_id=self.branch.id
        ).sudo().create(
            {
                "move_type": "out_invoice",
                "partner_id": self.customer.id,
                "company_id": self.company.id,
                "journal_id": self.invoice_journal.id,
                "invoice_date": fields.Date.today(),
                "date": fields.Date.today(),
            }
        )
        preorder.with_user(cashier)._redate_original_payments_to_invoice(invoice)

        payment.invalidate_recordset(["date", "state", "move_id"])
        self.assertEqual(payment.date, fields.Date.today())
        self.assertEqual(payment.move_id.state, "posted")
        self.assertEqual(payment.name, original_name)
        self.assertEqual(payment.amount, original_amount)
        self.assertEqual(payment.journal_id, original_journal)

    def test_branch_cashier_can_use_preorder_delivery_in_session_opened_by_another_user(self):
        workflow_groups = (
            self.env.ref("base.group_user")
            | self.env.ref("point_of_sale.group_pos_user")
            | self.env.ref("branch.group_branch_user")
            | self.env.ref("preorder_management.group_preorder_user")
        )
        cashier = self.env["res.users"].with_context(
            no_reset_password=True
        ).sudo().create(
            {
                "name": "Automated Secondary POS Cashier",
                "login": "automated_secondary_pos_cashier",
                "email": "automated_secondary_pos_cashier@example.com",
                "company_id": self.company.id,
                "company_ids": [Command.set(self.company.ids)],
                "branch_id": self.branch.id,
                "branch_ids": [Command.set(self.branch.ids)],
                "groups_id": [Command.set(workflow_groups.ids)],
            }
        )
        config = self.env["pos.config"].sudo().create(
            {
                "name": "Automated Shared-Session Pre-order POS",
                "company_id": self.company.id,
                "branch_id": self.branch.id,
                "enable_preorder_delivery": True,
            }
        )
        session = self.env["pos.session"].sudo().create(
            {"config_id": config.id, "user_id": self.env.user.id}
        )

        authorized_config, authorized_session = (
            self.env["sale.preorder"]
            .with_user(cashier)
            ._get_authorized_pos_delivery_context(config.id)
        )

        self.assertEqual(authorized_config, config)
        self.assertEqual(authorized_session, session)
        self.assertNotEqual(session.user_id, cashier)

    def test_campaign_quota_matrix_generation(self):
        second_product = self.product.copy(
            {"name": "Automated Pre-order Matrix Device"}
        )
        company_branches = self.env["res.branch"].search(
            [("company_id", "=", self.company.id)]
        )
        campaign = self.env["sale.preorder.campaign"].sudo().create(
            {
                "name": "Automated Quota Matrix Test",
                "company_id": self.company.id,
                "date_start": fields.Date.today(),
                "date_end": fields.Date.today() + timedelta(days=30),
                "product_ids": [Command.set((self.product | second_product).ids)],
                "select_all_branches": True,
                "default_branch_quota": 5.0,
            }
        )

        campaign.action_generate_allocation_lines()
        self.assertEqual(campaign.branch_ids, company_branches)
        self.assertEqual(
            len(campaign.allocation_line_ids), len(company_branches) * 2
        )
        self.assertEqual(set(campaign.allocation_line_ids.mapped("allocated_qty")), {5.0})

        edited_line = campaign.allocation_line_ids[:1]
        edited_line.allocated_qty = 8.0
        campaign.default_branch_quota = 7.0
        campaign.action_generate_allocation_lines()
        self.assertEqual(edited_line.allocated_qty, 8.0)
        self.assertEqual(
            len(campaign.allocation_line_ids), len(company_branches) * 2
        )

        campaign.product_ids = [Command.set(self.product.ids)]
        campaign.action_generate_allocation_lines()
        self.assertEqual(len(campaign.allocation_line_ids), len(company_branches))
        self.assertEqual(campaign.allocation_line_ids.product_id, self.product)

    def test_customer_preorder_list_uses_export_safe_report_columns(self):
        preorder_view = self.env.ref("preorder_management.sale_preorder_view_list")
        report_menu = self.env.ref("preorder_management.sale_preorder_menu_report")

        expected_columns = [
            ("name", "Pre-order"),
            ("preorder_date", "Date"),
            ("customer_id", "Customer"),
            ("branch_id", "Branch"),
            ("sales_rep_id", "Sales Rep"),
            ("discount_id", "Discount Reason"),
            ("device_summary", "Requested Device(s)"),
            ("requested_qty_total", "Total Quantity"),
            ("prepaid_amount", "Original Payment"),
            ("payment_method_1", "Journal 1"),
            ("payment_method_2", "Journal 2"),
        ]
        for field_name, label in expected_columns:
            self.assertIn(
                'name="%s" string="%s"' % (field_name, label),
                preorder_view.arch_db,
            )
            self.assertEqual(self.env["sale.preorder"]._fields[field_name].string, label)
        self.assertNotIn('widget="html"', preorder_view.arch_db)
        self.assertFalse(report_menu.active)

    def test_customer_preorder_search_anything_finds_customer_device_and_journal(self):
        self.customer.write({"phone": "+20-SEARCH-45819"})
        self.product.write({"default_code": "DEVICE-SEARCH-45819"})
        preorder = self.env["sale.preorder"].sudo().create(
            {
                "campaign_id": self.campaign.id,
                "customer_id": self.customer.id,
                "branch_id": self.branch.id,
                "sales_rep_id": self.sales_rep.id,
                "line_ids": [
                    Command.create(
                        {"product_id": self.product.id, "requested_qty": 1.0}
                    )
                ],
            }
        )
        preorder.action_confirm_preorder()
        self._post_payment(preorder)

        preorder_model = self.env["sale.preorder"].sudo()
        for query in (
            "SEARCH-45819",
            "DEVICE-SEARCH-45819",
            self.payment_journal.code,
        ):
            self.assertIn(
                preorder,
                preorder_model.search([("search_text", "ilike", query)]),
            )

        search_view = self.env.ref("preorder_management.sale_preorder_view_search")
        self.assertLess(
            search_view.arch_db.index('name="search_text"'),
            search_view.arch_db.index('name="name"'),
        )

    def test_pos_preorder_payload_includes_sales_representative(self):
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

        payload = preorder._serialize_for_pos(include_lines=True)

        self.assertEqual(payload["sales_rep_id"], self.sales_rep.id)
        self.assertEqual(payload["sales_rep_name"], self.sales_rep.display_name)

    def test_delivery_payment_inverse_is_declared_on_preorder(self):
        delivery_payments = self.env["sale.preorder"]._fields["delivery_payment_ids"]

        self.assertEqual(delivery_payments.comodel_name, "account.payment")
        self.assertEqual(delivery_payments.inverse_name, "preorder_delivery_id")
        self.assertNotIn(
            "delivery_payment_ids",
            self.env["sale.preorder.payment.confirmation"]._fields,
        )

    def test_pos_validation_accepts_a_post_validation_action_when_picking_is_done(self):
        picking = Mock(state="done")
        result = {"type": "ir.actions.client", "tag": "do_multi_print"}

        self.assertTrue(
            self.env["sale.preorder"]._ensure_pos_picking_validation_completed(
                picking, result
            )
        )
        picking.invalidate_recordset.assert_called_once_with(["state"])

    def test_pos_validation_rejects_a_confirmation_action_while_picking_is_open(self):
        picking = Mock(state="assigned")
        result = {
            "type": "ir.actions.act_window",
            "name": "Create Backorder?",
            "res_model": "stock.backorder.confirmation",
        }

        with self.assertRaisesRegex(UserError, "Create Backorder"):
            self.env["sale.preorder"]._ensure_pos_picking_validation_completed(
                picking, result
            )

    def test_multi_device_preorder_uses_one_payment_and_two_quotas(self):
        second_product = self.product.copy(
            {"name": "Automated Second Pre-order Device"}
        )
        self.campaign.product_ids = [Command.link(second_product.id)]
        second_allocation = self.env["sale.preorder.allocation"].sudo().create(
            {
                "campaign_id": self.campaign.id,
                "branch_id": self.branch.id,
                "product_id": second_product.id,
                "allocated_qty": 5.0,
            }
        )
        preorder = self.env["sale.preorder"].sudo().create(
            {
                "campaign_id": self.campaign.id,
                "customer_id": self.customer.id,
                "branch_id": self.branch.id,
                "sales_rep_id": self.sales_rep.id,
                "line_ids": [
                    Command.create(
                        {"product_id": self.product.id, "requested_qty": 1.0}
                    ),
                    Command.create(
                        {"product_id": second_product.id, "requested_qty": 2.0}
                    ),
                ],
            }
        )
        self.assertEqual(len(preorder.line_ids), 2)
        self.assertEqual(preorder.requested_qty_total, 3.0)
        self.assertGreater(preorder.deposit_amount, 0.0)

        preorder.action_confirm_preorder()
        self._post_payment(preorder)
        self.assertEqual(preorder.state, "pending")
        self.assertTrue(preorder.is_reserved)
        self.assertFalse(preorder.allocation_id)
        self.assertEqual(
            preorder.allocation_ids,
            self.campaign.allocation_line_ids.filtered(
                lambda allocation: allocation.product_id
                in (self.product | second_product)
            ),
        )
        second_allocation.invalidate_recordset(["reserved_qty", "available_qty"])
        self.assertEqual(second_allocation.reserved_qty, 2.0)
        self.assertEqual(second_allocation.available_qty, 3.0)

        self.campaign.action_open_allocation_delivery()
        self.assertEqual(preorder.state, "allocated")
        delivery_values = preorder._prepare_delivery_order_values()
        self.assertEqual(len(delivery_values["order_line"]), 2)
        preorder.action_create_delivery_order()
        self.assertEqual(
            preorder.final_sale_order_id.order_line.product_id,
            self.product | second_product,
        )

        report_action = self.env.ref(
            "preorder_management.action_report_preorder_confirmation"
        )
        report_html, _ = report_action._render_qweb_html(
            report_action.report_name, preorder.ids
        )
        self.assertIn(b"Automated Second Pre-order Device", report_html)

    def test_production_access_groups_separate_admin_and_branch_scope(self):
        branch_group = self.env.ref("preorder_management.group_preorder_user")
        manager_group = self.env.ref("preorder_management.group_preorder_manager")
        self.assertIn(branch_group, manager_group.implied_ids)

        other_branch = self.env["res.branch"].sudo().create(
            {"name": "Automated Restricted Branch", "company_id": self.company.id}
        )
        branch_user = self.env["res.users"].with_context(
            no_reset_password=True
        ).sudo().create(
            {
                "name": "Automated Pre-order Branch User",
                "login": "automated_preorder_branch_user",
                "email": "automated_preorder_branch_user@example.com",
                "company_id": self.company.id,
                "company_ids": [Command.set(self.company.ids)],
                "branch_id": other_branch.id,
                "branch_ids": [Command.set(other_branch.ids)],
                "groups_id": [Command.set(branch_group.ids)],
            }
        )
        manager_user = self.env["res.users"].with_context(
            no_reset_password=True
        ).sudo().create(
            {
                "name": "Automated Pre-order Central Admin",
                "login": "automated_preorder_central_admin",
                "email": "automated_preorder_central_admin@example.com",
                "company_id": self.company.id,
                "company_ids": [Command.set(self.company.ids)],
                "groups_id": [Command.set(manager_group.ids)],
            }
        )
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
        self.assertFalse(
            self.env["sale.preorder"].with_user(branch_user).search(
                [("id", "=", preorder.id)]
            )
        )
        self.assertEqual(
            self.env["sale.preorder"].with_user(manager_user).search(
                [("id", "=", preorder.id)]
            ),
            preorder,
        )
        with self.assertRaises(AccessError):
            self.campaign.with_user(branch_user).write({"notes": "Not allowed"})

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
        self.assertNotIn(" / ", preorder.payment_method_breakdown)
        expected_breakdown_rows = 2 if self.second_payment_journal != self.payment_journal else 1
        self.assertEqual(
            str(preorder.payment_method_breakdown_html).count("text-nowrap"),
            expected_breakdown_rows,
        )
        self.assertIn(
            self.payment_journal.display_name,
            preorder.payment_method_1,
        )
        self.assertNotIn("<", preorder.payment_method_1)
        if self.second_payment_journal != self.payment_journal:
            self.assertIn(
                self.second_payment_journal.display_name,
                preorder.payment_method_2,
            )
            self.assertNotIn("<", preorder.payment_method_2)
        else:
            self.assertFalse(preorder.payment_method_2)
        self.assertFalse(preorder.payment_method_3)
        self.assertFalse(preorder.payment_method_4)
        self.assertFalse(preorder.additional_payment_methods)
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
        self.campaign.notes = "Bring the original ID and reservation receipt."
        report_html, _ = report_action._render_qweb_html(
            report_action.report_name, preorder.ids
        )
        self.assertIn(b"Reserved Device", report_html)
        self.assertIn(b"Total Paid", report_html)
        self.assertIn(b"Bring the original ID and reservation receipt.", report_html)
        self.assertIn(b">Payment<", report_html)
        self.assertNotIn(b">Journal<", report_html)
        self.assertNotIn(b">Payment Method<", report_html)
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

    def test_pos_serial_assignment_preflight(self):
        serial_product = self.product.copy(
            {
                "name": "Automated POS Pre-order Serial Device",
                "tracking": "serial",
            }
        )
        self.campaign.product_ids = [Command.link(serial_product.id)]
        self.env["sale.preorder.allocation"].sudo().create(
            {
                "campaign_id": self.campaign.id,
                "branch_id": self.branch.id,
                "product_id": serial_product.id,
                "allocated_qty": 2.0,
            }
        )
        preorder = self.env["sale.preorder"].sudo().create(
            {
                "campaign_id": self.campaign.id,
                "customer_id": self.customer.id,
                "branch_id": self.branch.id,
                "sales_rep_id": self.sales_rep.id,
                "line_ids": [
                    Command.create(
                        {"product_id": serial_product.id, "requested_qty": 1.0}
                    )
                ],
            }
        )
        lot = self.env["stock.lot"].sudo().create(
            {
                "name": "POS-PREORDER-SERIAL-001",
                "product_id": serial_product.id,
                "company_id": self.company.id,
            }
        )

        with self.assertRaisesRegex(UserError, "exactly 1 serial"):
            preorder._prepare_pos_serial_lots({}, self.env["pos.config"])
        with self.assertRaisesRegex(UserError, "Unknown serial"):
            preorder._prepare_pos_serial_lots(
                {str(serial_product.id): ["UNKNOWN-SERIAL"]},
                self.env["pos.config"],
            )

        result = preorder._prepare_pos_serial_lots(
            {str(serial_product.id): [lot.name]},
            self.env["pos.config"],
        )
        self.assertEqual(result[serial_product.id], lot)

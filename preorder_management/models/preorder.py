# -*- coding: utf-8 -*-

from odoo import Command, api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import float_compare, float_is_zero


ACTIVE_ALLOCATION_STATES = ("allocated", "delivery", "invoiced", "completed")


def _check_preorder_manager(env):
    if not env.su and not env.user.has_group(
        "preorder_management.group_preorder_manager"
    ):
        raise AccessError(_("Only Pre-order Managers can perform this action."))


class SalePreorderCampaign(models.Model):
    _name = "sale.preorder.campaign"
    _description = "Pre-order Campaign"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "date_start desc, id desc"

    name = fields.Char(required=True, tracking=True)
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )
    date_start = fields.Date(required=True, tracking=True)
    date_end = fields.Date(required=True, tracking=True)
    state = fields.Selection(
        [
            ("draft", "Setup"),
            ("open", "Taking Pre-orders"),
            ("allocation", "Allocation"),
            ("delivery", "Delivery"),
            ("closed", "Closed"),
            ("cancelled", "Cancelled"),
        ],
        default="draft",
        required=True,
        readonly=True,
        tracking=True,
        index=True,
    )
    product_ids = fields.Many2many(
        "product.product",
        "sale_preorder_campaign_product_rel",
        "campaign_id",
        "product_id",
        string="Pre-order Products",
        domain="[('sale_ok', '=', True)]",
        help="Products available for this pre-order campaign. Products are required before allocation.",
    )
    branch_ids = fields.Many2many(
        "res.branch",
        "sale_preorder_campaign_branch_rel",
        "campaign_id",
        "branch_id",
        string="Participating Branches",
        domain="[('company_id', '=', company_id)]",
        help="Leave empty to include every branch in the company.",
    )
    allocation_line_ids = fields.One2many(
        "sale.preorder.allocation", "campaign_id", string="Branch Allocations"
    )
    preorder_ids = fields.One2many(
        "sale.preorder", "campaign_id", string="Customer Pre-orders"
    )
    preorder_count = fields.Integer(compute="_compute_campaign_totals")
    requested_quantity = fields.Float(compute="_compute_campaign_totals")
    allocated_quantity = fields.Float(compute="_compute_campaign_totals")
    delivered_quantity = fields.Float(compute="_compute_campaign_totals")
    notes = fields.Html()

    @api.depends("preorder_ids.state", "preorder_ids.requested_qty")
    def _compute_campaign_totals(self):
        for campaign in self:
            active = campaign.preorder_ids.filtered(lambda record: record.state != "cancelled")
            allocated = active.filtered(lambda record: record.state in ACTIVE_ALLOCATION_STATES)
            delivered = active.filtered(lambda record: record.state == "completed")
            campaign.preorder_count = len(active)
            campaign.requested_quantity = sum(active.mapped("requested_qty"))
            campaign.allocated_quantity = sum(allocated.mapped("requested_qty"))
            campaign.delivered_quantity = sum(delivered.mapped("requested_qty"))

    @api.constrains("date_start", "date_end")
    def _check_campaign_dates(self):
        for campaign in self:
            if campaign.date_start and campaign.date_end and campaign.date_end < campaign.date_start:
                raise ValidationError(_("Campaign end date cannot be earlier than its start date."))

    def action_open_campaign(self):
        _check_preorder_manager(self.env)
        self.write({"state": "open"})

    def action_start_allocation(self):
        _check_preorder_manager(self.env)
        for campaign in self:
            if not campaign.product_ids:
                raise UserError(_("Select the campaign products before starting allocation."))
            if not campaign.allocation_line_ids:
                raise UserError(_("Add at least one branch allocation before starting allocation."))
            invalid_lines = campaign.allocation_line_ids.filtered(
                lambda line: (
                    (campaign.product_ids and line.product_id not in campaign.product_ids)
                    or (campaign.branch_ids and line.branch_id not in campaign.branch_ids)
                )
            )
            if invalid_lines:
                raise UserError(_("Every quota line must use a participating branch and campaign product."))
        self.write({"state": "allocation"})

    def action_start_delivery(self):
        _check_preorder_manager(self.env)
        self.write({"state": "delivery"})

    def action_close_campaign(self):
        _check_preorder_manager(self.env)
        for campaign in self:
            unfinished = campaign.preorder_ids.filtered(
                lambda record: record.state not in ("completed", "cancelled")
            )
            if unfinished:
                raise UserError(
                    _("This campaign still has %s unfinished pre-order(s).") % len(unfinished)
                )
        self.write({"state": "closed"})

    def action_cancel_campaign(self):
        _check_preorder_manager(self.env)
        for campaign in self:
            progressed = campaign.preorder_ids.filtered(
                lambda record: record.state in ("delivery", "invoiced", "completed")
            )
            if progressed:
                raise UserError(
                    _("A campaign with delivery orders or invoices cannot be cancelled.")
                )
            campaign.preorder_ids.filtered(
                lambda record: record.state != "cancelled"
            )._workflow_write({"allocation_id": False, "state": "cancelled"})
        self.write({"state": "cancelled"})

    def action_reset_to_setup(self):
        _check_preorder_manager(self.env)
        for campaign in self:
            if campaign.preorder_ids.filtered(
                lambda record: record.state in ("delivery", "invoiced", "completed")
            ):
                raise UserError(_("Reset is blocked after delivery processing has started."))
        self.write({"state": "draft"})

    def action_open_preorders(self):
        self.ensure_one()
        action = self.env["ir.actions.actions"]._for_xml_id(
            "preorder_management.sale_preorder_action"
        )
        action["domain"] = [("campaign_id", "=", self.id)]
        action["context"] = {"default_campaign_id": self.id}
        return action


class SalePreorderAllocation(models.Model):
    _name = "sale.preorder.allocation"
    _description = "Pre-order Branch Allocation"
    _order = "branch_id, product_id"

    campaign_id = fields.Many2one(
        "sale.preorder.campaign", required=True, ondelete="cascade", index=True
    )
    company_id = fields.Many2one(related="campaign_id.company_id", store=True, index=True)
    branch_id = fields.Many2one(
        "res.branch", required=True, domain="[('company_id', '=', company_id)]", index=True
    )
    product_id = fields.Many2one(
        "product.product", required=True, domain="[('sale_ok', '=', True)]", index=True
    )
    allocated_qty = fields.Float(string="Branch Quota", required=True, default=0.0)
    reserved_qty = fields.Float(compute="_compute_quantities", string="Reserved")
    delivered_qty = fields.Float(compute="_compute_quantities", string="Delivered")
    available_qty = fields.Float(compute="_compute_quantities", string="Available")
    preorder_ids = fields.One2many("sale.preorder", "allocation_id", string="Pre-orders")

    _sql_constraints = [
        (
            "campaign_branch_product_unique",
            "unique(campaign_id, branch_id, product_id)",
            "Only one quota line is allowed per campaign, branch, and product.",
        )
    ]

    @api.depends("allocated_qty", "preorder_ids.state", "preorder_ids.requested_qty")
    def _compute_quantities(self):
        for allocation in self:
            reserved = allocation.preorder_ids.filtered(
                lambda record: record.state in ACTIVE_ALLOCATION_STATES
            )
            delivered = reserved.filtered(lambda record: record.state == "completed")
            allocation.reserved_qty = sum(reserved.mapped("requested_qty"))
            allocation.delivered_qty = sum(delivered.mapped("requested_qty"))
            allocation.available_qty = allocation.allocated_qty - allocation.reserved_qty

    @api.constrains("allocated_qty")
    def _check_allocated_quantity(self):
        for allocation in self:
            if float_compare(allocation.allocated_qty, 0.0, precision_digits=2) < 0:
                raise ValidationError(_("Branch quota cannot be negative."))
            if float_compare(
                allocation.allocated_qty, allocation.reserved_qty, precision_digits=2
            ) < 0:
                raise ValidationError(
                    _("Branch quota cannot be reduced below the already reserved quantity.")
                )

    @api.constrains("campaign_id", "branch_id", "product_id")
    def _check_campaign_scope(self):
        for allocation in self:
            if allocation.branch_id.company_id != allocation.company_id:
                raise ValidationError(_("Allocation branch must belong to the campaign company."))
            if allocation.campaign_id.branch_ids and allocation.branch_id not in allocation.campaign_id.branch_ids:
                raise ValidationError(_("Allocation branch is not participating in this campaign."))
            if allocation.campaign_id.product_ids and allocation.product_id not in allocation.campaign_id.product_ids:
                raise ValidationError(_("Allocation product is not included in this campaign."))


class SalePreorder(models.Model):
    _name = "sale.preorder"
    _description = "Customer Pre-order"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "preorder_date desc, id desc"

    name = fields.Char(default="New", required=True, readonly=True, copy=False, index=True)
    campaign_id = fields.Many2one(
        "sale.preorder.campaign", required=True, ondelete="restrict", tracking=True, index=True
    )
    campaign_company_id = fields.Many2one(
        related="campaign_id.company_id", string="Campaign Company"
    )
    source_order_id = fields.Many2one(
        "sale.order",
        string="Pre-order Quotation",
        required=True,
        ondelete="restrict",
        tracking=True,
        index=True,
        domain="[('inv_type', '=', 'quotation'), ('company_id', '=', campaign_company_id)]",
    )
    company_id = fields.Many2one(related="source_order_id.company_id", store=True, index=True)
    currency_id = fields.Many2one(related="source_order_id.currency_id", store=True)
    customer_id = fields.Many2one(
        "res.partner", string="Customer", related="source_order_id.partner_id", store=True, index=True
    )
    branch_id = fields.Many2one(
        "res.branch", string="Branch", related="source_order_id.branch_id", store=True, index=True
    )
    preorder_date = fields.Datetime(
        string="Date", related="source_order_id.date_order", store=True, index=True
    )
    sales_rep_id = fields.Many2one(
        "sales.rep", string="Sales Rep", related="source_order_id.sales_rep_id", store=True, index=True
    )
    discount_id = fields.Many2one(
        "discount.reason",
        string="Discount Reason",
        related="source_order_id.discount_id",
        store=True,
        index=True,
    )
    source_state = fields.Selection(related="source_order_id.state", string="Quotation Status")
    source_amount_total = fields.Monetary(
        related="source_order_id.amount_total", string="Pre-order Amount"
    )

    product_id = fields.Many2one(
        "product.product",
        string="Requested Product",
        domain="[('sale_ok', '=', True)] + (campaign_product_ids and [('id', 'in', campaign_product_ids)] or [])",
        tracking=True,
    )
    campaign_product_ids = fields.Many2many(
        "product.product", related="campaign_id.product_ids", string="Campaign Products"
    )
    requested_qty = fields.Float(string="Requested Qty", required=True, default=1.0, tracking=True)
    price_unit = fields.Monetary(string="Delivery Unit Price", tracking=True)
    discount = fields.Float(string="Delivery Discount (%)", digits=(16, 6), tracking=True)
    allocation_id = fields.Many2one(
        "sale.preorder.allocation", readonly=True, copy=False, tracking=True
    )
    final_sale_order_id = fields.Many2one(
        "sale.order", string="Delivery Sales Order", readonly=True, copy=False, tracking=True
    )
    invoice_ids = fields.One2many("account.move", "preorder_id", string="Delivery Invoices")
    invoice_count = fields.Integer(compute="_compute_document_counts")
    payment_count = fields.Integer(compute="_compute_payment_summary")
    payment_ids = fields.Many2many(
        "account.payment",
        "sale_preorder_original_payment_rel",
        "preorder_id",
        "payment_id",
        compute="_compute_payment_summary",
    )
    applied_payment_ids = fields.Many2many(
        "account.payment",
        "sale_preorder_applied_payment_rel",
        "preorder_id",
        "payment_id",
        compute="_compute_applied_payment_summary",
    )
    prepaid_amount = fields.Monetary(compute="_compute_payment_summary", string="Original Payment")
    available_prepayment_amount = fields.Monetary(
        compute="_compute_payment_summary", string="Available Prepayment"
    )
    prepayment_applied_amount = fields.Monetary(
        compute="_compute_applied_payment_summary", string="Applied to Invoice"
    )
    payment_method_names = fields.Char(compute="_compute_payment_summary", string="Payment Method(s)")
    payment_status = fields.Selection(
        [
            ("none", "No Payment"),
            ("available", "Available"),
            ("part_used", "Partly Used"),
            ("used", "Used"),
            ("returned", "Returned"),
        ],
        compute="_compute_payment_summary",
        store=True,
        index=True,
    )
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("pending", "Pending Allocation"),
            ("allocated", "Allocated"),
            ("delivery", "Delivery Order"),
            ("invoiced", "Payment Due"),
            ("completed", "Completed"),
            ("cancelled", "Cancelled"),
        ],
        default="draft",
        required=True,
        readonly=True,
        tracking=True,
        index=True,
    )
    notes = fields.Text()

    _sql_constraints = [
        (
            "source_order_unique",
            "unique(source_order_id)",
            "This quotation is already linked to a pre-order record.",
        ),
        (
            "requested_qty_positive",
            "check(requested_qty > 0)",
            "Requested quantity must be greater than zero.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not self.env.su and (
                vals.get("state", "draft") != "draft"
                or vals.get("allocation_id")
                or vals.get("final_sale_order_id")
            ):
                raise UserError(_("Workflow fields can only be changed through pre-order actions."))
            if vals.get("name", "New") == "New":
                vals["name"] = self.env["ir.sequence"].next_by_code("sale.preorder") or "New"
        records = super().create(vals_list)
        records._validate_source_scope()
        return records

    def write(self, vals):
        workflow_fields = {"state", "allocation_id", "final_sale_order_id"}
        if (
            workflow_fields & set(vals)
            and not self.env.su
            and not self.env.context.get("allow_preorder_workflow_write")
        ):
            raise UserError(_("Use the pre-order workflow buttons to change status or allocation."))
        protected = {"campaign_id", "source_order_id", "product_id", "requested_qty"}
        if protected & set(vals) and self.filtered(
            lambda record: record.state not in ("draft", "pending", "cancelled")
        ):
            raise UserError(_("Campaign, quotation, product, and quantity cannot change after allocation."))
        result = super().write(vals)
        if {"campaign_id", "source_order_id", "product_id"} & set(vals):
            self._validate_source_scope()
        return result

    def _workflow_write(self, vals):
        return self.with_context(allow_preorder_workflow_write=True).write(vals)

    def unlink(self):
        if self.filtered(lambda record: record.state not in ("draft", "cancelled")):
            raise UserError(_("Only draft or cancelled pre-orders can be deleted."))
        return super().unlink()

    @api.onchange("source_order_id", "product_id", "requested_qty")
    def _onchange_delivery_price(self):
        for record in self:
            if not record.product_id:
                continue
            price = record.product_id.lst_price
            pricelist = record.source_order_id.pricelist_id
            if pricelist:
                price = pricelist._get_product_price(
                    record.product_id,
                    record.requested_qty or 1.0,
                    currency=record.currency_id,
                    date=record.source_order_id.date_order,
                    uom=record.product_id.uom_id,
                )
            record.price_unit = price

    @api.depends("invoice_ids", "invoice_ids.state", "final_sale_order_id")
    def _compute_document_counts(self):
        for record in self:
            record.invoice_count = len(record.invoice_ids.filtered(lambda move: move.state != "cancel"))

    @api.depends(
        "source_order_id.payment_ids",
        "source_order_id.payment_ids.amount",
        "source_order_id.payment_ids.state",
        "source_order_id.payment_ids.move_id.state",
        "source_order_id.payment_ids.move_id.line_ids.amount_residual",
        "source_order_id.payment_ids.move_id.line_ids.amount_residual_currency",
        "source_order_id.payment_ids.reversed_original_payment_id",
    )
    def _compute_payment_summary(self):
        for record in self:
            all_inbound = record._get_source_inbound_payments(include_returned=True)
            usable = record._get_source_inbound_payments()
            returned = all_inbound - usable
            available_lines = record._get_available_payment_lines(usable)

            record.payment_ids = all_inbound
            record.payment_count = len(all_inbound)
            record.prepaid_amount = sum(
                record._convert_payment_amount(payment) for payment in all_inbound
            )
            record.available_prepayment_amount = sum(
                record._payment_line_residual_in_order_currency(line) for line in available_lines
            )
            record.payment_method_names = ", ".join(
                dict.fromkeys(
                    payment.journal_id.display_name
                    for payment in all_inbound
                    if payment.journal_id
                )
            )
            if not all_inbound:
                record.payment_status = "none"
            elif returned and not usable:
                record.payment_status = "returned"
            elif available_lines and float_compare(
                record.available_prepayment_amount,
                record.prepaid_amount,
                precision_rounding=record.currency_id.rounding,
            ) < 0:
                record.payment_status = "part_used"
            elif available_lines:
                record.payment_status = "available"
            else:
                record.payment_status = "used"

    @api.depends(
        "invoice_ids.payment_state",
        "invoice_ids.amount_residual",
        "invoice_ids.line_ids.matched_credit_ids",
        "invoice_ids.line_ids.matched_debit_ids",
        "source_order_id.payment_ids",
    )
    def _compute_applied_payment_summary(self):
        for record in self:
            source_payments = record._get_source_inbound_payments(include_returned=True)
            applied = self.env["account.payment"]
            amount = 0.0
            for invoice in record.invoice_ids.filtered(lambda move: move.state == "posted"):
                reconciled = invoice._get_reconciled_payments() & source_payments
                applied |= reconciled
                if hasattr(invoice, "_get_all_reconciled_invoice_partials"):
                    for values in invoice._get_all_reconciled_invoice_partials():
                        counterpart = self.env["account.move.line"].browse(values.get("aml_id"))
                        if counterpart.move_id.origin_payment_id in source_payments:
                            amount += abs(values.get("amount") or 0.0)
            record.applied_payment_ids = applied
            record.prepayment_applied_amount = amount

    def _convert_payment_amount(self, payment):
        self.ensure_one()
        if payment.currency_id == self.currency_id:
            return abs(payment.amount)
        return payment.currency_id._convert(
            abs(payment.amount),
            self.currency_id,
            self.company_id,
            payment.date or fields.Date.context_today(self),
        )

    def _payment_line_residual_in_order_currency(self, line):
        self.ensure_one()
        if line.currency_id == self.currency_id and line.amount_residual_currency:
            return abs(line.amount_residual_currency)
        return self.company_id.currency_id._convert(
            abs(line.amount_residual),
            self.currency_id,
            self.company_id,
            line.date or fields.Date.context_today(self),
        )

    def _get_source_inbound_payments(self, include_returned=False):
        self.ensure_one()
        payments = self.source_order_id.payment_ids.filtered(
            lambda payment: payment.payment_type == "inbound"
            and payment.state in ("in_process", "paid")
            and payment.move_id
            and payment.move_id.state == "posted"
        )
        if include_returned:
            return payments
        returned_originals = self.source_order_id.payment_ids.filtered(
            lambda payment: payment.payment_type == "outbound"
            and payment.state in ("in_process", "paid")
            and payment.move_id
            and payment.move_id.state == "posted"
            and payment.reversed_original_payment_id
        ).mapped("reversed_original_payment_id")
        return payments - returned_originals

    def _get_available_payment_lines(self, payments=None):
        self.ensure_one()
        payments = payments if payments is not None else self._get_source_inbound_payments()
        lines = payments.mapped("move_id.line_ids").filtered(
            lambda line: line.account_id.account_type == "asset_receivable"
            and line.account_id.reconcile
            and not line.reconciled
            and not float_is_zero(
                line.amount_residual,
                precision_rounding=line.company_id.currency_id.rounding,
            )
            and float_compare(
                line.amount_residual,
                0.0,
                precision_rounding=line.company_id.currency_id.rounding,
            ) < 0
        )
        return lines.sorted(lambda line: (line.date or fields.Date.today(), line.id))

    def _validate_source_scope(self):
        for record in self:
            source = record.source_order_id
            campaign = record.campaign_id
            if source.company_id != campaign.company_id:
                raise ValidationError(_("Pre-order quotation and campaign must use the same company."))
            if campaign.branch_ids and source.branch_id not in campaign.branch_ids:
                raise ValidationError(_("The quotation branch is not participating in this campaign."))
            if record.product_id and campaign.product_ids and record.product_id not in campaign.product_ids:
                raise ValidationError(_("Requested product is not included in this campaign."))
            if source.inv_type != "quotation" or not source._has_downpayment_product_lines():
                raise ValidationError(
                    _("The source must be a quotation containing a Down Payment product line.")
                )

    def action_confirm_preorder(self):
        for record in self:
            record._validate_source_scope()
            if record.campaign_id.state not in ("open", "allocation"):
                raise UserError(_("The campaign is not accepting pre-orders."))
            if record.source_order_id.state not in ("draft", "sent", "to_use"):
                raise UserError(_("The source quotation is cancelled, refunded, or already processed."))
            if not record.product_id:
                raise UserError(_("Select the requested product before confirming the pre-order."))
            if not record._get_available_payment_lines():
                raise UserError(
                    _("No reusable posted payment is available on the source quotation.")
                )
            record._workflow_write({"state": "pending"})
        return True

    def action_allocate(self):
        _check_preorder_manager(self.env)
        for record in self:
            self.env.cr.execute(
                "SELECT id FROM sale_preorder WHERE id = %s FOR UPDATE", [record.id]
            )
            record.invalidate_recordset(["state", "allocation_id"])
            if record.state != "pending":
                raise UserError(_("Only pending pre-orders can be allocated."))
            if record.campaign_id.state not in ("allocation", "delivery"):
                raise UserError(_("The campaign is not in allocation or delivery."))
            allocation = self.env["sale.preorder.allocation"].search(
                [
                    ("campaign_id", "=", record.campaign_id.id),
                    ("branch_id", "=", record.branch_id.id),
                    ("product_id", "=", record.product_id.id),
                ],
                limit=1,
            )
            if not allocation:
                raise UserError(_("No branch quota exists for this product and branch."))

            self.env.cr.execute(
                "SELECT id FROM sale_preorder_allocation WHERE id = %s FOR UPDATE",
                [allocation.id],
            )
            already_reserved = sum(
                self.search(
                    [
                        ("allocation_id", "=", allocation.id),
                        ("state", "in", ACTIVE_ALLOCATION_STATES),
                        ("id", "!=", record.id),
                    ]
                ).mapped("requested_qty")
            )
            remaining = allocation.allocated_qty - already_reserved
            if float_compare(
                remaining, record.requested_qty, precision_digits=2
            ) < 0:
                raise UserError(
                    _("Only %(remaining)s unit(s) remain in this branch quota; %(requested)s requested.")
                    % {"remaining": remaining, "requested": record.requested_qty}
                )
            record._workflow_write({"allocation_id": allocation.id, "state": "allocated"})
        return True

    def action_unallocate(self):
        _check_preorder_manager(self.env)
        for record in self:
            if record.state != "allocated":
                raise UserError(_("Only allocated pre-orders can be returned to the queue."))
            record._workflow_write({"allocation_id": False, "state": "pending"})
        return True

    def _prepare_delivery_order_values(self):
        self.ensure_one()
        source = self.source_order_id
        product = self.product_id
        if float_compare(self.price_unit, 0.0, precision_rounding=self.currency_id.rounding) <= 0:
            raise UserError(_("Set a positive delivery unit price before creating the delivery order."))

        taxes = product.taxes_id.filtered(lambda tax: tax.company_id == self.company_id)
        if source.fiscal_position_id:
            taxes = source.fiscal_position_id.map_tax(taxes)
        description = product.get_product_multiline_description_sale() or product.display_name
        line_values = {
            "product_id": product.id,
            "name": description,
            "product_uom_qty": self.requested_qty,
            "product_uom": product.uom_id.id,
            "price_unit": self.price_unit,
            "discount": self.discount,
            "tax_id": [Command.set(taxes.ids)],
        }
        if "discount_reason_id" in self.env["sale.order.line"]._fields and self.discount_id:
            line_values["discount_reason_id"] = self.discount_id.id

        values = {
            "partner_id": self.customer_id.id,
            "partner_invoice_id": source.partner_invoice_id.id,
            "partner_shipping_id": source.partner_shipping_id.id,
            "company_id": self.company_id.id,
            "branch_id": self.branch_id.id,
            "warehouse_id": source.warehouse_id.id,
            "pricelist_id": source.pricelist_id.id,
            "payment_term_id": source.payment_term_id.id,
            "fiscal_position_id": source.fiscal_position_id.id,
            "team_id": source.team_id.id,
            "user_id": source.user_id.id,
            "sales_rep_id": self.sales_rep_id.id,
            "discount_id": self.discount_id.id,
            "reference_number": source.reference_number,
            "inv_type": "invoice",
            "preorder_id": self.id,
            "preorder_source_order_id": source.id,
            "client_order_ref": _("Pre-order %s") % source.name,
            "order_line": [Command.create(line_values)],
        }
        if source.invoice_journal_id:
            values["invoice_journal_id"] = source.invoice_journal_id.id
        return values

    def action_create_delivery_order(self):
        self.ensure_one()
        self.env.cr.execute(
            "SELECT id FROM sale_preorder WHERE id = %s FOR UPDATE", [self.id]
        )
        self.invalidate_recordset(["state", "final_sale_order_id"])
        if self.final_sale_order_id:
            return self.action_open_delivery_order()
        if self.state != "allocated":
            raise UserError(_("Allocate the pre-order before creating its delivery order."))
        if self.campaign_id.state != "delivery":
            raise UserError(_("The campaign must be in Delivery before orders are created."))
        if not self._get_available_payment_lines():
            raise UserError(_("The original payment is no longer available for this delivery."))

        delivery_order = self.env["sale.order"].create(self._prepare_delivery_order_values())
        self._workflow_write({"final_sale_order_id": delivery_order.id, "state": "delivery"})
        self.message_post(
            body=_("Draft delivery sales order %s was created. Verify price and serial/delivery before invoicing.")
            % delivery_order.display_name
        )
        return self.action_open_delivery_order()

    def _apply_original_payments(self, invoices):
        self.ensure_one()
        payments = self._get_source_inbound_payments()
        payment_lines = self._get_available_payment_lines(payments)
        if not payments:
            raise UserError(_("The original pre-order payment was returned or is unavailable."))
        invalid_payments = payments.filtered(
            lambda payment: payment.company_id != self.company_id
            or payment.partner_id.commercial_partner_id != self.customer_id.commercial_partner_id
            or (payment.branch_id and payment.branch_id != self.branch_id)
        )
        if invalid_payments:
            raise UserError(
                _(
                    "Every original payment must belong to the same company, customer, and branch "
                    "as the delivery invoice."
                )
            )

        for invoice in invoices.sorted(lambda move: (move.invoice_date or fields.Date.today(), move.id)):
            if invoice.company_id != self.company_id:
                raise UserError(_("Invoice and pre-order payment belong to different companies."))
            if invoice.branch_id != self.branch_id:
                raise UserError(_("Invoice and pre-order payment belong to different branches."))
            if invoice.commercial_partner_id != self.customer_id.commercial_partner_id:
                raise UserError(_("Invoice and pre-order payment belong to different customers."))
            if invoice.currency_id != self.currency_id:
                raise UserError(_("Invoice and pre-order payment must use the same currency."))

            while payment_lines and not float_is_zero(
                invoice.amount_residual, precision_rounding=invoice.currency_id.rounding
            ):
                invoice_accounts = invoice.line_ids.filtered(
                    lambda line: line.account_id.account_type == "asset_receivable"
                    and not line.reconciled
                ).mapped("account_id")
                line = next(
                    (candidate for candidate in payment_lines if candidate.account_id in invoice_accounts),
                    False,
                )
                if not line:
                    raise UserError(
                        _(
                            "The pre-order payment uses a different receivable/outstanding account. "
                            "Accounting must move it to the same customer receivable account before it can be reused."
                        )
                    )
                invoice.js_assign_outstanding_line(line.id)
                payment_lines = self._get_available_payment_lines(payments)

    def action_invoice_and_apply_payment(self):
        self.ensure_one()
        self.env.cr.execute(
            "SELECT id FROM sale_preorder WHERE id = %s FOR UPDATE", [self.id]
        )
        self.invalidate_recordset(["state", "final_sale_order_id", "invoice_ids"])
        order = self.final_sale_order_id
        if not order:
            raise UserError(_("Create the delivery sales order first."))
        if order.state not in ("sale", "done"):
            raise UserError(_("Confirm the delivery sales order before invoicing."))

        open_pickings = order.picking_ids.filtered(lambda picking: picking.state != "cancel")
        if open_pickings and open_pickings.filtered(lambda picking: picking.state != "done"):
            raise UserError(
                _("Validate the delivery and serial number before creating the customer invoice.")
            )

        invoices = self.invoice_ids.filtered(
            lambda move: move.move_type == "out_invoice" and move.state != "cancel"
        )
        if order.invoice_status == "to invoice" or not invoices:
            invoices |= order._create_invoices()
        drafts = invoices.filtered(lambda move: move.state == "draft")
        if drafts:
            drafts.action_post()
        posted = invoices.filtered(lambda move: move.state == "posted")
        if not posted:
            raise UserError(_("No posted customer invoice is available for payment application."))

        self._apply_original_payments(posted)
        residual = sum(posted.mapped("amount_residual"))
        completed = float_is_zero(residual, precision_rounding=self.currency_id.rounding)
        self._workflow_write({"state": "completed" if completed else "invoiced"})
        self.message_post(
            body=(
                _("Original pre-order payment applied. Delivery invoice is fully paid.")
                if completed
                else _("Original pre-order payment applied. Remaining invoice balance: %s %s")
                % (residual, self.currency_id.name)
            )
        )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Pre-order payment applied"),
                "message": (
                    _("The original payment settled the delivery invoice without a refund or second charge.")
                    if completed
                    else _("The original payment was applied; %s %s remains due.")
                    % (residual, self.currency_id.name)
                ),
                "type": "success" if completed else "warning",
                "sticky": True,
                "next": self.action_open_invoices(),
            },
        }

    def action_cancel_preorder(self):
        for record in self:
            if record.state in ("invoiced", "completed"):
                raise UserError(_("A pre-order with a posted delivery invoice cannot be cancelled here."))
            if record.final_sale_order_id and record.final_sale_order_id.state != "cancel":
                raise UserError(_("Cancel the delivery sales order before cancelling this pre-order."))
            if record.invoice_ids.filtered(lambda move: move.state == "posted"):
                raise UserError(_("Reverse or cancel the posted delivery invoice first."))
            record._workflow_write({"allocation_id": False, "state": "cancelled"})
        return True

    def action_reset_draft(self):
        _check_preorder_manager(self.env)
        for record in self:
            if record.final_sale_order_id:
                raise UserError(_("A pre-order with a delivery order cannot be reset."))
            record._workflow_write({"allocation_id": False, "state": "draft"})
        return True

    def action_open_source_order(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Pre-order Quotation"),
            "res_model": "sale.order",
            "view_mode": "form",
            "res_id": self.source_order_id.id,
        }

    def action_open_payments(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Original Pre-order Payments"),
            "res_model": "account.payment",
            "view_mode": "list,form",
            "domain": [("id", "in", self.payment_ids.ids)],
        }

    def action_open_delivery_order(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Delivery Sales Order"),
            "res_model": "sale.order",
            "view_mode": "form",
            "res_id": self.final_sale_order_id.id,
        }

    def action_open_invoices(self):
        self.ensure_one()
        action = self.env["ir.actions.actions"]._for_xml_id("account.action_move_out_invoice_type")
        action["domain"] = [("id", "in", self.invoice_ids.ids)]
        if len(self.invoice_ids) == 1:
            action.update({"view_mode": "form", "res_id": self.invoice_ids.id, "views": [(False, "form")]})
        return action

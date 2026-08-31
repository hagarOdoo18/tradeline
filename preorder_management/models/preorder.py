# -*- coding: utf-8 -*-

from odoo import Command, api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import float_compare, float_is_zero


def _check_preorder_manager(env):
    if not env.su and not env.user.has_group(
        "preorder_management.group_preorder_manager"
    ):
        raise AccessError(_("Only Pre-order Central Admins can perform this action."))


def _check_preorder_branch_access(records):
    """Allow Central Admins globally and Branch Managers only in their branches."""
    env = records.env
    if env.su or env.user.has_group("preorder_management.group_preorder_manager"):
        return
    if not env.user.has_group("preorder_management.group_preorder_user"):
        raise AccessError(_("Only Pre-order Branch Managers can perform this action."))
    outside_branches = records.filtered(
        lambda record: record.branch_id not in env.user.branch_ids
    )
    if outside_branches:
        raise AccessError(_("You can only process pre-orders for your assigned branches."))


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
    quota_quantity = fields.Float(
        compute="_compute_campaign_totals", string="Total Branch Quota"
    )
    allocated_quantity = fields.Float(
        compute="_compute_campaign_totals", string="Reserved Quantity"
    )
    available_quantity = fields.Float(
        compute="_compute_campaign_totals", string="Available Quantity"
    )
    delivered_quantity = fields.Float(compute="_compute_campaign_totals")
    notes = fields.Html()

    @api.depends(
        "allocation_line_ids.allocated_qty",
        "preorder_ids.state",
        "preorder_ids.requested_qty",
        "preorder_ids.allocation_id",
    )
    def _compute_campaign_totals(self):
        for campaign in self:
            active = campaign.preorder_ids.filtered(lambda record: record.state != "cancelled")
            allocated = active.filtered("allocation_id")
            delivered = active.filtered(lambda record: record.state == "completed")
            quota_quantity = sum(campaign.allocation_line_ids.mapped("allocated_qty"))
            reserved_quantity = sum(allocated.mapped("requested_qty"))
            campaign.preorder_count = len(active)
            campaign.requested_quantity = sum(active.mapped("requested_qty"))
            campaign.quota_quantity = quota_quantity
            campaign.allocated_quantity = reserved_quantity
            campaign.available_quantity = quota_quantity - reserved_quantity
            campaign.delivered_quantity = sum(delivered.mapped("requested_qty"))

    @api.constrains("date_start", "date_end")
    def _check_campaign_dates(self):
        for campaign in self:
            if campaign.date_start and campaign.date_end and campaign.date_end < campaign.date_start:
                raise ValidationError(_("Campaign end date cannot be earlier than its start date."))

    def action_open_campaign(self):
        _check_preorder_manager(self.env)
        self._validate_allocation_setup()
        self.write({"state": "open"})

    def action_start_allocation(self):
        _check_preorder_manager(self.env)
        self._validate_allocation_setup()
        self.write({"state": "allocation"})

    def _validate_allocation_setup(self):
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
        return True

    def action_open_allocation_delivery(self):
        """Open delivery work and activate every paid reservation automatically."""
        _check_preorder_manager(self.env)
        invalid_state = self.filtered(lambda campaign: campaign.state != "open")
        if invalid_state:
            raise UserError(_("Only campaigns taking pre-orders can be opened for allocation and delivery."))
        self._validate_allocation_setup()
        self.write({"state": "delivery"})
        reserved_count, waiting_count = self._auto_reserve_pending_preorders()
        return self._reservation_notification(reserved_count, waiting_count)

    def _auto_reserve_pending_preorders(self):
        """Make paid reservations ready for delivery when the admin opens delivery."""
        reserved_count = 0
        waiting_count = 0
        oldest = fields.Datetime.to_datetime("1970-01-01 00:00:00")
        for campaign in self:
            pending = campaign.preorder_ids.filtered(
                lambda record: record.state == "pending"
            ).sorted(lambda record: (record.preorder_date or oldest, record.id))
            for preorder in pending:
                reserved, reason = preorder._reserve_from_branch_quota(
                    strict=False, mark_ready=True
                )
                if reserved:
                    reserved_count += 1
                else:
                    waiting_count += 1
                    preorder.message_post(
                        body=_(
                            "Automatic reservation is waiting for Central Admin action: %s"
                        )
                        % reason
                    )
        return reserved_count, waiting_count

    def _reservation_notification(self, reserved_count, waiting_count):
        message = _("%(reserved)s paid reservation(s) are ready for delivery.") % {
            "reserved": reserved_count
        }
        notification_type = "success"
        sticky = False
        if waiting_count:
            message += _(
                " %(waiting)s paid pre-order(s) could not be activated because a reservation is missing. Correct the quota and retry."
            ) % {"waiting": waiting_count}
            notification_type = "warning"
            sticky = True
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Reserved Pre-orders"),
                "message": message,
                "type": notification_type,
                "sticky": sticky,
            },
        }

    def action_retry_auto_reservations(self):
        _check_preorder_manager(self.env)
        invalid_state = self.filtered(
            lambda campaign: campaign.state not in ("allocation", "delivery")
        )
        if invalid_state:
            raise UserError(_("Automatic reservation can only run during allocation or delivery."))
        self._validate_allocation_setup()
        reserved_count, waiting_count = self._auto_reserve_pending_preorders()
        return self._reservation_notification(reserved_count, waiting_count)

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
            if campaign.preorder_ids.filtered(lambda record: record.state != "cancelled"):
                raise UserError(_("Reset is blocked while the campaign has active pre-orders."))
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
                lambda record: record.state != "cancelled"
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

    def _default_branch_id(self):
        user = self.env.user
        if user.branch_id and user.branch_id.company_id == self.env.company:
            return user.branch_id
        company_branches = user.branch_ids.filtered(
            lambda branch: branch.company_id == self.env.company
        )
        return company_branches[:1]

    def _default_sales_rep_id(self):
        branch = self._default_branch_id()
        if not branch:
            return False
        return self.env["sales.rep"].search(
            [
                ("name", "=", branch.name),
                "|",
                ("branch_id", "=", branch.id),
                ("branch_id", "=", False),
            ],
            limit=1,
        )

    def _get_downpayment_product(self):
        return self.env["product.product"].search(
            [
                ("sale_ok", "=", True),
                "|",
                ("name", "=ilike", "Down Payment"),
                ("name", "=ilike", "Downpayment"),
            ],
            order="id",
            limit=1,
        )

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
        ondelete="restrict",
        tracking=True,
        index=True,
        domain="[('inv_type', '=', 'quotation'), ('company_id', '=', campaign_company_id)]",
        copy=False,
        readonly=True,
    )
    company_id = fields.Many2one(related="campaign_id.company_id", store=True, index=True)
    currency_id = fields.Many2one("res.currency", compute="_compute_currency")
    customer_id = fields.Many2one(
        "res.partner",
        string="Customer",
        required=True,
        tracking=True,
        index=True,
        domain="[('company_id', 'in', [False, company_id])]",
    )
    branch_id = fields.Many2one(
        "res.branch",
        string="Branch",
        required=True,
        default=_default_branch_id,
        tracking=True,
        index=True,
        domain="[('company_id', '=', company_id)]",
    )
    preorder_date = fields.Datetime(
        string="Date",
        required=True,
        default=fields.Datetime.now,
        tracking=True,
        index=True,
    )
    sales_rep_id = fields.Many2one(
        "sales.rep",
        string="Sales Rep",
        required=True,
        default=_default_sales_rep_id,
        tracking=True,
        index=True,
        domain="['|', ('branch_id', '=', branch_id), ('branch_id', '=', False)]",
    )
    discount_id = fields.Many2one(
        "discount.reason",
        string="Discount Reason",
        tracking=True,
        index=True,
    )
    source_state = fields.Selection(related="source_order_id.state", string="Quotation Status")
    source_amount_total = fields.Monetary(
        related="source_order_id.amount_total", string="Pre-order Amount"
    )
    deposit_amount = fields.Monetary(
        string="Required Payment Total",
        compute="_compute_required_payment_total",
        store=True,
        tracking=True,
        help="Full customer total after the normal line discount and taxes.",
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
    price_unit = fields.Monetary(
        string="Unit Price",
        compute="_compute_price_unit",
        store=True,
        tracking=True,
        help="Locked product price calculated from the customer's pricelist.",
    )
    discount = fields.Float(string="Discount (%)", digits=(16, 6), tracking=True)
    allocation_id = fields.Many2one(
        "sale.preorder.allocation", readonly=True, copy=False, tracking=True
    )
    final_sale_order_id = fields.Many2one(
        "sale.order", string="Delivery Sales Order", readonly=True, copy=False, tracking=True
    )
    invoice_ids = fields.One2many("account.move", "preorder_id", string="Delivery Invoices")
    direct_payment_ids = fields.One2many(
        "account.payment", "preorder_payment_id", string="Direct Pre-order Payments"
    )
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
    payment_due_amount = fields.Monetary(
        compute="_compute_payment_summary", string="Payment Due"
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
            ("confirmed", "Awaiting Payment"),
            ("pending", "Paid — Reserved, Waiting Delivery"),
            ("allocated", "Reserved — Ready for Delivery"),
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
        (
            "discount_percentage_range",
            "check(discount >= 0 AND discount <= 100)",
            "Discount must be between 0 and 100 percent.",
        ),
    ]

    def init(self):
        """Backfill quotation-first records when upgrading to the pre-order-first workflow."""
        self.env.cr.execute(
            """
            UPDATE sale_preorder AS preorder
               SET customer_id = COALESCE(preorder.customer_id, source.partner_id),
                   branch_id = COALESCE(preorder.branch_id, source.branch_id),
                   preorder_date = COALESCE(preorder.preorder_date, source.date_order),
                   sales_rep_id = COALESCE(preorder.sales_rep_id, source.sales_rep_id),
                   discount_id = COALESCE(preorder.discount_id, source.discount_id),
                   deposit_amount = CASE
                       WHEN COALESCE(preorder.deposit_amount, 0) <= 0 THEN source.amount_total
                       ELSE preorder.deposit_amount
                   END
              FROM sale_order AS source
             WHERE preorder.source_order_id = source.id
            """
        )
        self.env.cr.execute(
            """
            UPDATE sale_order AS source
               SET reference_number = preorder.name,
                   client_order_ref = COALESCE(source.client_order_ref, preorder.name)
              FROM sale_preorder AS preorder
             WHERE preorder.source_order_id = source.id
               AND source.reference_number IS DISTINCT FROM preorder.name
            """
        )

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
            source = self.env["sale.order"].browse(vals.get("source_order_id")).exists()
            if source:
                vals.setdefault("customer_id", source.partner_id.id)
                vals.setdefault("branch_id", source.branch_id.id)
                vals.setdefault("preorder_date", source.date_order)
                vals.setdefault("sales_rep_id", source.sales_rep_id.id)
                vals.setdefault("discount_id", source.discount_id.id)
        records = super().create(vals_list)
        if records.filtered(lambda record: record.campaign_id.state != "open"):
            raise UserError(_("New customer pre-orders can only be created while the campaign is Taking Pre-orders."))
        records._validate_preorder_scope()
        records._validate_available_quota_for_draft()
        return records

    def write(self, vals):
        workflow_fields = {"state", "allocation_id", "final_sale_order_id"}
        if (
            workflow_fields & set(vals)
            and not self.env.su
            and not self.env.context.get("allow_preorder_workflow_write")
        ):
            raise UserError(_("Use the pre-order workflow buttons to change status or allocation."))
        protected = {
            "campaign_id",
            "customer_id",
            "branch_id",
            "product_id",
            "requested_qty",
            "sales_rep_id",
            "discount_id",
            "discount",
        }
        if protected & set(vals) and self.filtered(
            lambda record: record.state != "draft"
        ):
            raise UserError(
                _(
                    "Customer, branch, product, quantity, Sales Rep, Discount, and Discount "
                    "Reason cannot change after the pre-order is confirmed."
                )
            )
        result = super().write(vals)
        if {
            "campaign_id",
            "source_order_id",
            "customer_id",
            "branch_id",
            "product_id",
            "requested_qty",
            "sales_rep_id",
        } & set(vals):
            self._validate_preorder_scope()
        if {"campaign_id", "branch_id", "product_id", "requested_qty"} & set(vals):
            self.filtered(lambda record: record.state == "draft")._validate_available_quota_for_draft()
        return result

    def _workflow_write(self, vals):
        return self.with_context(allow_preorder_workflow_write=True).write(vals)

    def unlink(self):
        if self.filtered(lambda record: record.state not in ("draft", "cancelled")):
            raise UserError(_("Only draft or cancelled pre-orders can be deleted."))
        return super().unlink()

    @api.depends(
        "source_order_id.currency_id",
        "customer_id.property_product_pricelist.currency_id",
        "company_id.currency_id",
    )
    def _compute_currency(self):
        for record in self:
            pricelist = record.customer_id.property_product_pricelist
            record.currency_id = (
                record.source_order_id.currency_id
                or pricelist.currency_id
                or record.company_id.currency_id
            )

    @api.onchange("branch_id")
    def _onchange_branch_id(self):
        for record in self:
            if not record.branch_id:
                continue
            if record.sales_rep_id and (
                not record.sales_rep_id.branch_id
                or record.sales_rep_id.branch_id == record.branch_id
            ):
                continue
            record.sales_rep_id = self.env["sales.rep"].search(
                [
                    ("name", "=", record.branch_id.name),
                    "|",
                    ("branch_id", "=", record.branch_id.id),
                    ("branch_id", "=", False),
                ],
                limit=1,
            )

    @api.depends(
        "customer_id",
        "customer_id.property_product_pricelist",
        "product_id",
        "requested_qty",
        "preorder_date",
        "company_id",
    )
    def _compute_price_unit(self):
        for record in self:
            if not record.product_id:
                record.price_unit = 0.0
                continue
            price = record.product_id.lst_price
            pricelist = (
                record.source_order_id.pricelist_id
                or record.customer_id.property_product_pricelist
            )
            if pricelist:
                price = pricelist._get_product_price(
                    record.product_id,
                    record.requested_qty or 1.0,
                    currency=pricelist.currency_id,
                    date=record.preorder_date or fields.Datetime.now(),
                    uom=record.product_id.uom_id,
                )
            record.price_unit = price

    def _get_fiscal_position(self):
        self.ensure_one()
        if not self.customer_id:
            return self.env["account.fiscal.position"]
        return self.env["account.fiscal.position"].with_company(
            self.company_id
        )._get_fiscal_position(self.customer_id)

    def _get_product_taxes(self):
        self.ensure_one()
        if not self.product_id:
            return self.env["account.tax"]
        taxes = self.product_id.taxes_id.filtered(
            lambda tax: tax.company_id == self.company_id
        )
        fiscal_position = self._get_fiscal_position()
        return fiscal_position.map_tax(taxes) if fiscal_position else taxes

    @api.depends(
        "product_id",
        "requested_qty",
        "price_unit",
        "discount",
        "customer_id",
        "company_id",
        "currency_id",
    )
    def _compute_required_payment_total(self):
        for record in self:
            if not record.product_id or not record.currency_id:
                record.deposit_amount = 0.0
                continue
            discounted_unit_price = record.price_unit * (1.0 - record.discount / 100.0)
            totals = record._get_product_taxes().compute_all(
                discounted_unit_price,
                currency=record.currency_id,
                quantity=record.requested_qty,
                product=record.product_id,
                partner=record.customer_id,
            )
            record.deposit_amount = totals["total_included"]

    @api.depends("invoice_ids", "invoice_ids.state", "final_sale_order_id")
    def _compute_document_counts(self):
        for record in self:
            record.invoice_count = len(record.invoice_ids.filtered(lambda move: move.state != "cancel"))

    @api.depends(
        "deposit_amount",
        "direct_payment_ids",
        "direct_payment_ids.amount",
        "direct_payment_ids.state",
        "direct_payment_ids.move_id.state",
        "direct_payment_ids.move_id.line_ids.amount_residual",
        "direct_payment_ids.move_id.line_ids.amount_residual_currency",
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
            usable_paid_amount = sum(
                record._convert_payment_amount(payment) for payment in usable
            )
            record.available_prepayment_amount = sum(
                record._payment_line_residual_in_order_currency(line) for line in available_lines
            )
            record.payment_due_amount = max(record.deposit_amount - usable_paid_amount, 0.0)
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
        "direct_payment_ids",
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
        # Search explicitly instead of relying on the cached One2many value. A
        # payment is linked while the pre-order form is already in cache, so the
        # inverse relation can otherwise still look empty during action_post().
        payments = self.env["account.payment"].search(
            [
                ("preorder_payment_id", "=", self.id),
                ("payment_type", "=", "inbound"),
                ("state", "in", ("in_process", "paid", "posted")),
                ("move_id.state", "=", "posted"),
            ]
        )
        if self.source_order_id:
            payments |= self.source_order_id.payment_ids.filtered(
                lambda payment: payment.payment_type == "inbound"
                and payment.state in ("in_process", "paid", "posted")
                and payment.move_id
                and payment.move_id.state == "posted"
            )
        if include_returned:
            return payments
        returned_originals = self.env["account.payment"].search(
            [
                ("payment_type", "=", "outbound"),
                ("state", "in", ("in_process", "paid", "posted")),
                ("move_id.state", "=", "posted"),
                ("reversed_original_payment_id", "in", payments.ids),
            ]
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

    def _validate_preorder_scope(self):
        for record in self:
            campaign = record.campaign_id
            if record.branch_id.company_id != campaign.company_id:
                raise ValidationError(_("Pre-order branch and campaign must use the same company."))
            if campaign.branch_ids and record.branch_id not in campaign.branch_ids:
                raise ValidationError(_("The pre-order branch is not participating in this campaign."))
            if record.product_id and campaign.product_ids and record.product_id not in campaign.product_ids:
                raise ValidationError(_("Requested product is not included in this campaign."))
            if record.sales_rep_id.branch_id and record.sales_rep_id.branch_id != record.branch_id:
                raise ValidationError(_("Sales Rep must belong to the selected branch."))
            if not self.env.su and not self.env.user.has_group(
                "preorder_management.group_preorder_manager"
            ) and record.branch_id not in self.env.user.branch_ids:
                raise AccessError(_("You can only create pre-orders for one of your assigned branches."))

            source = record.source_order_id
            if not source:
                continue
            if source.company_id != campaign.company_id:
                raise ValidationError(_("Pre-order quotation and campaign must use the same company."))
            if source.branch_id != record.branch_id:
                raise ValidationError(_("Pre-order quotation and customer request must use the same branch."))
            if source.partner_id != record.customer_id:
                raise ValidationError(_("Pre-order quotation and customer request must use the same customer."))
            if source.reference_number != record.name:
                raise ValidationError(_("Quotation Reference Number must match the Customer Pre-order number."))
            if source.inv_type != "quotation" or not source._has_downpayment_product_lines():
                raise ValidationError(
                    _("The source must be a quotation containing a Down Payment product line.")
                )

    def _validate_available_quota_for_draft(self):
        """Reject a draft immediately when its branch/product quota is exhausted."""
        for record in self.filtered("product_id"):
            allocation = self.env["sale.preorder.allocation"].search(
                [
                    ("campaign_id", "=", record.campaign_id.id),
                    ("branch_id", "=", record.branch_id.id),
                    ("product_id", "=", record.product_id.id),
                ],
                limit=1,
            )
            if not allocation:
                raise UserError(
                    _(
                        "No quota is configured for %(product)s at %(branch)s. "
                        "Choose another product or branch, or ask the Central Admin to add quota."
                    )
                    % {
                        "product": record.product_id.display_name,
                        "branch": record.branch_id.display_name,
                    }
                )
            allocation.invalidate_recordset(["reserved_qty", "available_qty"])
            if float_compare(
                allocation.available_qty,
                record.requested_qty,
                precision_digits=2,
            ) < 0:
                raise UserError(
                    _(
                        "Only %(available)s unit(s) remain for %(product)s at %(branch)s, "
                        "but this pre-order requests %(requested)s. No new pre-order can be "
                        "created until quota is released or increased."
                    )
                    % {
                        "available": allocation.available_qty,
                        "product": record.product_id.display_name,
                        "branch": record.branch_id.display_name,
                        "requested": record.requested_qty,
                    }
                )
        return True

    def _prepare_source_order_values(self):
        self.ensure_one()
        if not self.customer_id:
            raise UserError(_("Select the customer before creating the Sales Order."))
        if not self.sales_rep_id:
            raise UserError(_("Select the Sales Rep before creating the Sales Order."))
        if float_compare(
            self.deposit_amount,
            0.0,
            precision_rounding=self.currency_id.rounding,
        ) <= 0:
            raise UserError(_("Set a positive Deposit Amount before creating the Sales Order."))

        downpayment_product = self._get_downpayment_product()
        if not downpayment_product:
            raise UserError(_("No saleable Down Payment product is configured."))
        warehouse = self.env["stock.warehouse"].search(
            [
                ("company_id", "=", self.company_id.id),
                ("branch_id", "=", self.branch_id.id),
            ],
            order="id",
            limit=1,
        )
        if not warehouse:
            raise UserError(_("No warehouse is configured for branch %s.") % self.branch_id.display_name)
        invoice_journal = self.env["account.journal"].search(
            [
                ("company_id", "=", self.company_id.id),
                ("branch_id", "=", self.branch_id.id),
                ("type", "=", "sale"),
            ],
            order="id",
            limit=1,
        )
        if not invoice_journal:
            raise UserError(_("No sales invoice journal is configured for branch %s.") % self.branch_id.display_name)

        pricelist = self.customer_id.property_product_pricelist
        if not pricelist:
            pricelist = self.env["product.pricelist"].search(
                [("company_id", "in", (False, self.company_id.id))],
                order="company_id desc, id",
                limit=1,
            )
        if not pricelist:
            raise UserError(_("No sales pricelist is available for this customer."))

        addresses = self.customer_id.address_get(["invoice", "delivery"])
        team = self.env["crm.team"].search(
            [("branch_id", "=", self.branch_id.id)], order="id", limit=1
        )
        taxes = downpayment_product.taxes_id.filtered(
            lambda tax: tax.company_id == self.company_id
        )
        line_values = {
            "product_id": downpayment_product.id,
            "name": downpayment_product.get_product_multiline_description_sale()
            or downpayment_product.display_name,
            "product_uom_qty": 1.0,
            "product_uom": downpayment_product.uom_id.id,
            "price_unit": self.deposit_amount,
            "tax_id": [Command.set(taxes.ids)],
        }
        values = {
            "partner_id": self.customer_id.id,
            "partner_invoice_id": addresses.get("invoice") or self.customer_id.id,
            "partner_shipping_id": addresses.get("delivery") or self.customer_id.id,
            "company_id": self.company_id.id,
            "branch_id": self.branch_id.id,
            "warehouse_id": warehouse.id,
            "pricelist_id": pricelist.id,
            "sales_rep_id": self.sales_rep_id.id,
            "discount_id": self.discount_id.id,
            "reference_number": self.name,
            "inv_type": "quotation",
            "invoice_journal_id": invoice_journal.id,
            "client_order_ref": self.name,
            "order_line": [Command.create(line_values)],
        }
        if team:
            values["team_id"] = team.id
        if self.customer_id.property_payment_term_id:
            values["payment_term_id"] = self.customer_id.property_payment_term_id.id
        return values

    def action_create_source_order(self):
        self.ensure_one()
        if self.source_order_id:
            return self.action_open_source_order()
        raise UserError(
            _(
                "Registration Sales Orders are no longer created. Confirm this Customer "
                "Pre-order and register its payment directly here."
            )
        )

    def action_register_payment(self):
        self.ensure_one()
        _check_preorder_branch_access(self)
        if self.state != "confirmed":
            raise UserError(_("Confirm the Customer Pre-order before registering payment."))
        self.invalidate_recordset(["payment_due_amount", "payment_status"])
        if float_is_zero(
            self.payment_due_amount, precision_rounding=self.currency_id.rounding
        ):
            raise UserError(_("This pre-order is already fully paid."))
        return {
            "name": _("Register Pre-order Payment"),
            "type": "ir.actions.act_window",
            "res_model": "account.payment",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_payment_type": "inbound",
                "default_partner_type": "customer",
                "default_partner_id": self.customer_id.id,
                "default_company_id": self.company_id.id,
                "default_currency_id": self.currency_id.id,
                "default_amount": self.payment_due_amount,
                "default_memo": self.name,
                "default_preorder_payment_id": self.id,
            },
        }

    def _sync_payment_readiness(self):
        """Move confirmed requests to the branch queue once the exact total is paid."""
        for record in self:
            record.invalidate_recordset(
                ["direct_payment_ids", "prepaid_amount", "payment_due_amount", "payment_status"]
            )
            usable_payments = record._get_source_inbound_payments()
            paid_amount = sum(
                record._convert_payment_amount(payment) for payment in usable_payments
            )
            comparison = float_compare(
                paid_amount,
                record.deposit_amount,
                precision_rounding=record.currency_id.rounding,
            )
            if comparison > 0:
                raise UserError(
                    _(
                        "Posted pre-order payments (%(paid)s %(currency)s) cannot exceed the "
                        "required total (%(required)s %(currency)s)."
                    )
                    % {
                        "paid": paid_amount,
                        "required": record.deposit_amount,
                        "currency": record.currency_id.name,
                    }
                )
            if comparison == 0 and record.state == "confirmed":
                record._workflow_write({"state": "pending"})
                record.message_post(
                    body=_(
                        "The full required payment was posted. The quantity is now being reserved automatically."
                    )
                )
                record._reserve_from_branch_quota(
                    strict=True,
                    mark_ready=record.campaign_id.state in ("allocation", "delivery"),
                )
            elif comparison < 0 and record.state in ("pending", "allocated"):
                record._workflow_write({"state": "confirmed"})
        return True

    def action_confirm_preorder(self):
        for record in self:
            _check_preorder_branch_access(record)
            record._validate_preorder_scope()
            if record.state != "draft":
                raise UserError(_("Only Draft pre-orders can be confirmed."))
            if record.campaign_id.state != "open":
                raise UserError(_("The campaign is not accepting pre-orders."))
            if not record.product_id:
                raise UserError(_("Select the requested product before confirming the pre-order."))
            if float_compare(
                record.price_unit, 0.0, precision_rounding=record.currency_id.rounding
            ) <= 0:
                raise UserError(_("The product pricelist must provide a positive Unit Price."))
            if record.discount and not record.discount_id:
                raise UserError(_("Select a Discount Reason when a Discount is entered."))
            if float_compare(
                record.deposit_amount, 0.0, precision_rounding=record.currency_id.rounding
            ) <= 0:
                raise UserError(_("The Required Payment Total must be positive."))
            record._workflow_write({"state": "confirmed"})
            record._sync_payment_readiness()
        return True

    def _reserve_from_branch_quota(self, strict=True, mark_ready=False):
        """Reserve quota after full payment; optionally mark it ready for delivery."""
        self.ensure_one()
        record = self
        self.env.cr.execute(
            "SELECT id FROM sale_preorder WHERE id = %s FOR UPDATE", [record.id]
        )
        record.invalidate_recordset(["state", "allocation_id"])
        if record.state == "cancelled":
            reason = _("the pre-order is cancelled")
            if strict:
                raise UserError(reason)
            return False, reason
        if record.campaign_id.state not in ("open", "allocation", "delivery"):
            reason = _("the campaign is not open")
            if strict:
                raise UserError(reason)
            return False, reason
        if record.allocation_id:
            if mark_ready and record.state == "pending":
                record._workflow_write({"state": "allocated"})
            return True, False

        allocation = self.env["sale.preorder.allocation"].search(
            [
                ("campaign_id", "=", record.campaign_id.id),
                ("branch_id", "=", record.branch_id.id),
                ("product_id", "=", record.product_id.id),
            ],
            limit=1,
        )
        if not allocation:
            reason = _("no quota exists for this product and branch")
            if strict:
                raise UserError(reason)
            return False, reason

        self.env.cr.execute(
            "SELECT id FROM sale_preorder_allocation WHERE id = %s FOR UPDATE",
            [allocation.id],
        )
        self.env.cr.execute(
            """
            SELECT COALESCE(SUM(requested_qty), 0.0)
             FROM sale_preorder
             WHERE allocation_id = %s
               AND state != 'cancelled'
               AND id != %s
            """,
            [allocation.id, record.id],
        )
        already_reserved = self.env.cr.fetchone()[0] or 0.0
        remaining = allocation.allocated_qty - already_reserved
        if float_compare(remaining, record.requested_qty, precision_digits=2) < 0:
            reason = _(
                "only %(remaining)s unit(s) remain in this branch quota; %(requested)s requested"
            ) % {"remaining": remaining, "requested": record.requested_qty}
            if strict:
                raise UserError(reason)
            return False, reason

        workflow_values = {"allocation_id": allocation.id}
        if mark_ready and record.state == "pending":
            workflow_values["state"] = "allocated"
        record._workflow_write(workflow_values)
        allocation.invalidate_recordset(["reserved_qty", "delivered_qty", "available_qty"])
        record.campaign_id.invalidate_recordset(
            ["allocated_quantity", "available_quantity"]
        )
        record.message_post(
            body=_(
                "Quantity reserved automatically from the %(branch)s quota for %(product)s."
            )
            % {
                "branch": record.branch_id.display_name,
                "product": record.product_id.display_name,
            }
        )
        return True, False

    def action_allocate(self):
        """Compatibility action; normal reservations are now automatic."""
        _check_preorder_branch_access(self)
        for record in self:
            record._reserve_from_branch_quota(strict=True, mark_ready=True)
        return True

    def action_unallocate(self):
        raise UserError(
            _("A reservation is released only when the pre-order is cancelled.")
        )

    def _prepare_delivery_order_values(self):
        self.ensure_one()
        source = self.source_order_id
        product = self.product_id
        if float_compare(self.price_unit, 0.0, precision_rounding=self.currency_id.rounding) <= 0:
            raise UserError(_("The product pricelist must provide a positive Unit Price."))

        warehouse = source.warehouse_id if source else self.env["stock.warehouse"].search(
            [
                ("company_id", "=", self.company_id.id),
                ("branch_id", "=", self.branch_id.id),
            ],
            order="id",
            limit=1,
        )
        if not warehouse:
            raise UserError(_("No warehouse is configured for branch %s.") % self.branch_id.display_name)
        invoice_journal = source.invoice_journal_id if source else self.env["account.journal"].search(
            [
                ("company_id", "=", self.company_id.id),
                ("branch_id", "=", self.branch_id.id),
                ("type", "=", "sale"),
            ],
            order="id",
            limit=1,
        )
        if not invoice_journal:
            raise UserError(_("No sales invoice journal is configured for branch %s.") % self.branch_id.display_name)
        pricelist = source.pricelist_id if source else self.customer_id.property_product_pricelist
        if not pricelist:
            pricelist = self.env["product.pricelist"].search(
                [("company_id", "in", (False, self.company_id.id))],
                order="company_id desc, id",
                limit=1,
            )
        if not pricelist:
            raise UserError(_("No sales pricelist is available for this customer."))

        addresses = self.customer_id.address_get(["invoice", "delivery"])
        fiscal_position = source.fiscal_position_id if source else self._get_fiscal_position()
        team = source.team_id if source else self.env["crm.team"].search(
            [("branch_id", "=", self.branch_id.id)], order="id", limit=1
        )
        payment_term = (
            source.payment_term_id
            if source
            else self.customer_id.property_payment_term_id
        )
        taxes = self._get_product_taxes()
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
            "partner_invoice_id": source.partner_invoice_id.id if source else addresses.get("invoice") or self.customer_id.id,
            "partner_shipping_id": source.partner_shipping_id.id if source else addresses.get("delivery") or self.customer_id.id,
            "company_id": self.company_id.id,
            "branch_id": self.branch_id.id,
            "warehouse_id": warehouse.id,
            "pricelist_id": pricelist.id,
            "payment_term_id": payment_term.id,
            "fiscal_position_id": fiscal_position.id,
            "team_id": team.id,
            "user_id": source.user_id.id if source else self.env.user.id,
            "sales_rep_id": self.sales_rep_id.id,
            "discount_id": self.discount_id.id,
            "reference_number": self.name,
            "inv_type": "invoice",
            "preorder_id": self.id,
            "preorder_source_order_id": source.id if source else False,
            "client_order_ref": self.name,
            "order_line": [Command.create(line_values)],
            "invoice_journal_id": invoice_journal.id,
        }
        return values

    def action_create_delivery_order(self):
        self.ensure_one()
        _check_preorder_branch_access(self)
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

    def _redate_original_payments_to_invoice(self, invoices):
        """Move the posted pre-order payments to the delivery invoice accounting date."""
        self.ensure_one()
        payments = self._get_source_inbound_payments()
        if not payments:
            raise UserError(_("The original pre-order payment was returned or is unavailable."))

        invoice_dates = set(invoices.mapped("date"))
        invoice_dates.discard(False)
        if len(invoice_dates) != 1:
            raise UserError(
                _(
                    "All delivery invoices must use the same accounting date before the original "
                    "payment can be re-dated."
                )
            )
        target_date = invoice_dates.pop()
        changed = []
        for payment in payments:
            if payment.date == target_date:
                continue
            if not payment.move_id or payment.move_id.state != "posted":
                raise UserError(
                    _("Payment %s does not have a posted journal entry.") % payment.display_name
                )
            old_date = payment.date
            try:
                payment.action_draft()
                payment.write({"date": target_date})
                payment.action_post()
            except Exception as error:
                raise UserError(
                    _(
                        "Payment %(payment)s could not be moved from %(old_date)s to "
                        "%(new_date)s. It may already be reconciled, hashed, or inside a locked "
                        "accounting period. Accounting must resolve that restriction before delivery.\n\n"
                        "Odoo detail: %(detail)s"
                    )
                    % {
                        "payment": payment.display_name,
                        "old_date": old_date,
                        "new_date": target_date,
                        "detail": error,
                    }
                ) from error
            payment.invalidate_recordset(["date", "state", "move_id"])
            if payment.move_id.state != "posted" or payment.date != target_date:
                raise UserError(
                    _("Payment %s was not reposted on the delivery invoice date.")
                    % payment.display_name
                )
            changed.append((payment.display_name, old_date, target_date))

        if changed:
            details = "<br/>".join(
                _("%(payment)s: %(old_date)s to %(new_date)s")
                % {
                    "payment": payment_name,
                    "old_date": old_date,
                    "new_date": new_date,
                }
                for payment_name, old_date, new_date in changed
            )
            self.message_post(
                body=_(
                    "Original payment journal date(s) changed to the delivery invoice "
                    "accounting date before reconciliation:<br/>%s"
                )
                % details
            )
        return target_date

    def action_invoice_and_apply_payment(self):
        self.ensure_one()
        _check_preorder_branch_access(self)
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

        self._redate_original_payments_to_invoice(posted)
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
                    _(
                        "The original payment was re-dated to the delivery invoice date and settled "
                        "the invoice without a refund or second charge."
                    )
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
            if record._get_source_inbound_payments(include_returned=True):
                raise UserError(_("Cancel or return the posted payment before resetting this pre-order."))
            record._workflow_write({"allocation_id": False, "state": "draft"})
        return True

    def action_open_source_order(self):
        self.ensure_one()
        if not self.source_order_id:
            raise UserError(_("No Sales Order has been created for this pre-order yet."))
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

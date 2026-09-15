# -*- coding: utf-8 -*-

from collections import defaultdict

from odoo import Command, api, fields, models, _
from odoo.exceptions import AccessError, UserError
from odoo.osv.expression import OR
from odoo.tools import float_compare, float_is_zero


class PosConfig(models.Model):
    _inherit = "pos.config"

    enable_preorder_delivery = fields.Boolean(
        string="Pre-order Delivery",
        default=False,
        help=(
            "Allow this POS to deliver fully paid pre-orders reserved for its branch. "
            "Migrated pre-orders are loaded into the normal cart so the branch can add items and "
            "collect one or more POS payments at delivery; legacy pre-orders continue to reuse their original payment."
        ),
    )


class PosSession(models.Model):
    _inherit = "pos.session"

    def _loader_params_pos_config(self):
        params = super()._loader_params_pos_config()
        fields_to_load = params["search_params"].setdefault("fields", [])
        if "enable_preorder_delivery" not in fields_to_load:
            fields_to_load.append("enable_preorder_delivery")
        return params


def _ready_preorder_records_for_config(env, config):
    """Return open, branch-owned pre-orders whose products/customers must be POS-loadable."""
    if not config.branch_id:
        return env["sale.preorder"]
    return env["sale.preorder"].sudo().search(
        [
            ("company_id", "=", config.company_id.id),
            ("branch_id", "=", config.branch_id.id),
            ("state", "=", "allocated"),
            ("campaign_id.state", "=", "delivery"),
        ]
    )


class ProductProductPreorderPos(models.Model):
    _inherit = "product.product"

    @api.model
    def _load_pos_data_domain(self, data):
        domain = super()._load_pos_data_domain(data)
        config = self.env["pos.config"].browse(data["pos.config"]["data"][0]["id"])
        products = _ready_preorder_records_for_config(self.env, config).mapped("line_ids.product_id")
        return OR([domain, [("id", "in", products.ids)]]) if products else domain

    def _load_pos_data(self, data):
        """Make ready pre-order products available even with limited POS loading.

        Odoo's standard limited-product loader does not use
        ``_load_pos_data_domain``.  Maadi has that optimisation enabled, so a
        branch-ready product could be visible in the pre-order dialog while its
        POS record was absent from the local product cache.  Append the small,
        branch-scoped reservation set after the standard loader in both modes.
        """
        result = super()._load_pos_data(data)
        config = self.env["pos.config"].browse(data["pos.config"]["data"][0]["id"])
        ready_products = _ready_preorder_records_for_config(self.env, config).mapped(
            "line_ids.product_id"
        )
        loaded_ids = {product["id"] for product in result["data"]}
        missing_ids = (ready_products.ids and set(ready_products.ids) - loaded_ids) or set()
        if missing_ids:
            result["data"].extend(
                self._load_product_with_domain([("id", "in", list(missing_ids))], config.id)
            )
        return result


class ResPartnerPreorderPos(models.Model):
    _inherit = "res.partner"

    @api.model
    def _load_pos_data_domain(self, data):
        domain = super()._load_pos_data_domain(data)
        config = self.env["pos.config"].browse(data["pos.config"]["data"][0]["id"])
        customers = _ready_preorder_records_for_config(self.env, config).mapped("customer_id")
        return OR([domain, [("id", "in", customers.ids)]]) if customers else domain


class SalePreorderPosDelivery(models.Model):
    _inherit = "sale.preorder"

    _POS_AUDIT_FIELDS = {
        "fulfilled_via",
        "fulfillment_pos_config_id",
        "fulfillment_pos_session_id",
        "fulfillment_pos_order_id",
        "fulfillment_user_id",
        "fulfilled_at",
        "pos_fulfillment_token",
    }

    fulfilled_via = fields.Selection(
        [("backend", "Sales"), ("pos", "Point of Sale")],
        string="Fulfilled Via",
        readonly=True,
        copy=False,
        tracking=True,
    )
    fulfillment_pos_config_id = fields.Many2one(
        "pos.config", string="Fulfillment POS", readonly=True, copy=False
    )
    fulfillment_pos_session_id = fields.Many2one(
        "pos.session", string="Fulfillment POS Session", readonly=True, copy=False
    )
    fulfillment_pos_order_id = fields.Many2one(
        "pos.order", string="Fulfillment POS Order", readonly=True, copy=False, index=True
    )
    fulfillment_user_id = fields.Many2one(
        "res.users", string="Fulfilled By", readonly=True, copy=False
    )
    fulfilled_at = fields.Datetime(readonly=True, copy=False)
    pos_fulfillment_token = fields.Char(readonly=True, copy=False, index=True)

    _sql_constraints = [
        (
            "pos_fulfillment_token_unique",
            "unique(pos_fulfillment_token)",
            "This POS fulfillment request was already processed.",
        ),
    ]

    def write(self, vals):
        if self._POS_AUDIT_FIELDS.intersection(vals) and not self.env.su:
            raise AccessError(_("Pre-order fulfillment audit fields are managed by the workflow."))
        return super().write(vals)

    @api.model
    def _get_authorized_pos_delivery_context(self, pos_config_id):
        if not self.env.user.has_group("point_of_sale.group_pos_user"):
            raise AccessError(_("Only Point of Sale users can deliver pre-orders from POS."))

        try:
            config_id = int(pos_config_id or 0)
        except (TypeError, ValueError):
            config_id = 0
        config = self.env["pos.config"].sudo().browse(config_id).exists()
        if not config:
            raise UserError(_("The Point of Sale configuration could not be identified."))
        if not config.enable_preorder_delivery:
            raise UserError(_("Pre-order Delivery is disabled for this Point of Sale."))
        if not config.branch_id:
            raise UserError(_("Set a branch on this Point of Sale before delivering pre-orders."))

        user = self.env.user
        allowed_branches = user.branch_ids | user.branch_id
        if not self.env.su and config.branch_id.id not in allowed_branches.ids:
            raise AccessError(_("This Point of Sale is outside your assigned branches."))
        if config.company_id.id not in user.company_ids.ids:
            raise AccessError(_("This Point of Sale is outside your allowed companies."))

        session = config.current_session_id
        if not session or session.state not in ("opening_control", "opened"):
            raise UserError(_("Open the Point of Sale session before delivering a pre-order."))
        return config, session

    @api.model
    def _get_preorder_for_pos(self, preorder_id, config):
        try:
            record_id = int(preorder_id or 0)
        except (TypeError, ValueError):
            record_id = 0
        preorder = self.sudo().browse(record_id).exists()
        if not preorder:
            raise UserError(_("The selected pre-order no longer exists."))
        if preorder.company_id != config.company_id or preorder.branch_id != config.branch_id:
            raise AccessError(_("The selected pre-order does not belong to this POS branch."))
        return preorder

    def _pos_available_payment_amount(self):
        self.ensure_one()
        if self.payment_recording_mode == "delivery":
            return self._get_delivery_payment_confirmed_amount()
        return sum(
            self._payment_line_residual_in_order_currency(line)
            for line in self._get_available_payment_lines()
        )

    def _check_pos_payment_ready(self):
        self.ensure_one()
        available = self._pos_available_payment_amount()
        comparison = float_compare(
            available,
            self.deposit_amount,
            precision_rounding=self.currency_id.rounding,
        )
        if comparison != 0:
            raise UserError(
                _(
                    "POS delivery requires an exactly fully paid pre-order. "
                    "Required: %(required).2f %(currency)s; available original payment: "
                    "%(available).2f %(currency)s."
                )
                % {
                    "required": self.deposit_amount,
                    "available": available,
                    "currency": self.currency_id.name,
                }
            )

    def _serialize_for_pos(self, include_lines=False):
        self.ensure_one()
        values = {
            "id": self.id,
            "name": self.name,
            "date": fields.Datetime.to_string(self.preorder_date),
            "customer_id": self.customer_id.id,
            "customer_name": self.customer_id.display_name,
            "customer_phone": self.customer_id.phone or self.customer_id.mobile or "",
            "device_summary": self.device_summary or "",
            "qty": self.requested_qty_total,
            "amount": self.deposit_amount,
            "amount_label": "%s %s" % (format(self.deposit_amount, ",.2f"), self.currency_id.name),
            "payment_method": self.payment_method_names or "",
            "notes": self.campaign_id.notes or "",
            "customer_notes": self.notes or "",
            "state": self.state,
            "payment_recording_mode": self.payment_recording_mode,
        }
        if include_lines:
            values["lines"] = [
                {
                    "id": line.id,
                    "product_id": line.product_id.id,
                    "product_name": line.product_id.display_name,
                    "qty": line.requested_qty,
                    "price_unit": line.price_unit,
                    "discount": line.discount,
                    "tracking": line.product_id.tracking,
                    "uom_rounding": line.product_id.uom_id.rounding,
                }
                for line in self.line_ids
            ]
        return values

    @api.model
    def _serialize_pos_payment_methods(self, config):
        methods = []
        for method in config.payment_method_ids.filtered(lambda item: item.journal_id):
            journal = method.journal_id
            inbound_line = journal.inbound_payment_method_line_ids[:1]
            if not inbound_line:
                continue
            methods.append(
                {
                    "id": method.id,
                    "name": method.name,
                    "journal_id": journal.id,
                    "journal_name": journal.display_name,
                }
            )
        return methods

    @api.model
    def get_ready_preorders_pos(self, pos_config_id, search_text=False, limit=120):
        config, _session = self._get_authorized_pos_delivery_context(pos_config_id)
        domain = [
            ("company_id", "=", config.company_id.id),
            ("branch_id", "=", config.branch_id.id),
            ("state", "=", "allocated"),
            ("campaign_id.state", "=", "delivery"),
        ]
        query = (search_text or "").strip()
        if query:
            domain += [
                "|", "|", "|",
                ("name", "ilike", query),
                ("customer_id.name", "ilike", query),
                ("customer_id.phone", "ilike", query),
                ("customer_id.mobile", "ilike", query),
            ]
        records = self.sudo().search(domain, order="preorder_date desc, id desc", limit=min(int(limit or 120), 200))
        ready = records.filtered(
            lambda preorder: float_is_zero(
                preorder._pos_available_payment_amount() - preorder.deposit_amount,
                precision_rounding=preorder.currency_id.rounding,
            )
        )
        return [record._serialize_for_pos() for record in ready]

    @api.model
    def get_preorder_delivery_details_pos(self, preorder_id, pos_config_id):
        config, _session = self._get_authorized_pos_delivery_context(pos_config_id)
        preorder = self._get_preorder_for_pos(preorder_id, config)
        if preorder.state != "allocated" or preorder.campaign_id.state != "delivery":
            raise UserError(_("This pre-order is no longer ready for delivery."))
        preorder._check_pos_payment_ready()
        non_stocked = preorder.line_ids.filtered(
            lambda line: not line.product_id.is_storable
        )
        if non_stocked:
            raise UserError(
                _("POS pre-order delivery requires stocked products: %s")
                % ", ".join(non_stocked.product_id.mapped("display_name"))
            )
        values = preorder._serialize_for_pos(include_lines=True)
        values["payment_methods"] = self._serialize_pos_payment_methods(config)
        return values


    def _create_pos_delivery_payments(self, invoice, payment_lines, config):
        """Post and reconcile branch-collected payment(s) dated at delivery."""
        self.ensure_one()
        if not isinstance(payment_lines, (list, tuple)) or not payment_lines:
            raise UserError(_("Enter at least one delivery payment."))
        methods = config.payment_method_ids.filtered(lambda item: item.journal_id)
        Payment = self.env["account.payment"].sudo()
        created = self.env["account.payment"]
        total = 0.0
        for line in payment_lines:
            if not isinstance(line, dict):
                raise UserError(_("The delivery payment details are invalid."))
            try:
                method_id = int(line.get("method_id") or 0)
                amount = float(line.get("amount") or 0.0)
            except (TypeError, ValueError) as error:
                raise UserError(_("The delivery payment details are invalid.")) from error
            method = methods.filtered(lambda item: item.id == method_id)[:1]
            if not method or amount <= 0:
                raise UserError(_("Select a valid POS payment method and positive amount."))
            if method.journal_id.company_id != self.company_id:
                raise UserError(_("Every delivery payment journal must belong to the pre-order company."))
            inbound_line = method.journal_id.inbound_payment_method_line_ids[:1]
            if not inbound_line:
                raise UserError(_("Journal %s has no inbound payment method configured.") % method.journal_id.display_name)
            payment = Payment.create(
                {
                    "payment_type": "inbound",
                    "partner_type": "customer",
                    "partner_id": self.customer_id.id,
                    "amount": amount,
                    "currency_id": self.currency_id.id,
                    "journal_id": method.journal_id.id,
                    "payment_method_line_id": inbound_line.id,
                    "branch_id": self.branch_id.id,
                    "sale_order_id": self.final_sale_order_id.id,
                    "preorder_delivery_id": self.id,
                    "memo": _("Pre-order delivery payment: %s") % self.name,
                }
            )
            payment.action_post()
            created |= payment
            total += amount
        if float_compare(total, self.deposit_amount, precision_rounding=self.currency_id.rounding) != 0:
            raise UserError(
                _(
                    "Delivery payments must equal %(required).2f %(currency)s; entered %(entered).2f."
                )
                % {"required": self.deposit_amount, "currency": self.currency_id.name, "entered": total}
            )
        for payment in created.sorted("id"):
            lines = payment.move_id.line_ids.filtered(
                lambda line: line.account_id.account_type == "asset_receivable"
                and not line.reconciled
                and line.amount_residual < 0
            )
            for line in lines:
                invoice.js_assign_outstanding_line(line.id)
        invoice.invalidate_recordset(["amount_residual", "payment_state"])
        if not float_is_zero(invoice.amount_residual, precision_rounding=invoice.currency_id.rounding):
            raise UserError(_("The delivery payment did not fully settle the invoice."))
        return created

    def _prepare_pos_serial_lots(self, serial_assignments, config):
        self.ensure_one()
        assignments = serial_assignments if isinstance(serial_assignments, dict) else {}
        lots_by_product = {}
        all_serial_names = []

        for line in self.line_ids:
            product = line.product_id
            if product.tracking == "lot":
                raise UserError(
                    _("Lot-tracked product %s cannot be delivered from this POS workflow yet.")
                    % product.display_name
                )
            if product.tracking != "serial":
                continue
            rounded_qty = round(line.requested_qty)
            if not float_is_zero(
                line.requested_qty - rounded_qty,
                precision_rounding=product.uom_id.rounding,
            ):
                raise UserError(_("Serial-tracked product quantities must be whole numbers."))
            raw_names = assignments.get(str(product.id), assignments.get(product.id, []))
            names = [str(value or "").strip() for value in raw_names]
            names = [name for name in names if name]
            if len(names) != rounded_qty:
                raise UserError(
                    _("Enter exactly %(qty)s serial number(s) for %(product)s.")
                    % {"qty": rounded_qty, "product": product.display_name}
                )
            if len(set(names)) != len(names):
                raise UserError(_("A serial number cannot be used more than once."))
            lots = self.env["stock.lot"].sudo().search(
                [
                    ("name", "in", names),
                    ("product_id", "=", product.id),
                    ("company_id", "in", (False, self.company_id.id)),
                ]
            )
            lots_by_name = {lot.name: lot for lot in lots}
            missing = [name for name in names if name not in lots_by_name]
            if missing:
                raise UserError(
                    _("Unknown serial number(s) for %(product)s: %(serials)s")
                    % {"product": product.display_name, "serials": ", ".join(missing)}
                )
            ordered_lots = self.env["stock.lot"].sudo()
            for name in names:
                ordered_lots |= lots_by_name[name]
            sold = ordered_lots.filtered(
                lambda lot: "serial_status" in lot._fields and lot.serial_status == "sold"
            )
            if sold:
                raise UserError(_("Serial number(s) already marked sold: %s") % ", ".join(sold.mapped("name")))
            lots_by_product[product.id] = ordered_lots
            all_serial_names.extend(names)

        if len(set(all_serial_names)) != len(all_serial_names):
            raise UserError(_("A serial number cannot be used for more than one device."))
        try:
            provided_product_ids = {int(key) for key, values in assignments.items() if values}
        except (TypeError, ValueError) as error:
            raise UserError(_("The serial assignment contains an invalid product.")) from error
        expected_product_ids = set(lots_by_product)
        if provided_product_ids - expected_product_ids:
            raise UserError(_("Serial numbers were supplied for a product outside this pre-order."))
        if lots_by_product:
            lot_ids = [lot.id for lots in lots_by_product.values() for lot in lots]
            self.env.cr.execute("SELECT id FROM stock_lot WHERE id IN %s FOR UPDATE", [tuple(lot_ids)])
        return lots_by_product

    def _set_pos_picking_quantities(self, order, lots_by_product, config):
        self.ensure_one()
        pickings = order.picking_ids.filtered(lambda picking: picking.state not in ("done", "cancel"))
        if not pickings:
            raise UserError(_("The delivery sales order did not create an open stock delivery."))
        pos_location = config.picking_type_id.default_location_src_id
        if not pos_location:
            raise UserError(_("The Point of Sale has no source stock location configured."))
        wrong_location = pickings.filtered(lambda picking: picking.location_id != pos_location)
        if wrong_location:
            raise UserError(
                _(
                    "The pre-order warehouse delivers from %(delivery_location)s, but this POS uses "
                    "%(pos_location)s. Align the POS and warehouse source locations first."
                )
                % {
                    "delivery_location": wrong_location[:1].location_id.complete_name,
                    "pos_location": pos_location.complete_name,
                }
            )

        pickings.action_assign()
        moves_by_product = defaultdict(lambda: self.env["stock.move"])
        for move in pickings.move_ids.filtered(lambda move: move.state not in ("done", "cancel")):
            moves_by_product[move.product_id.id] |= move

        for preorder_line in self.line_ids:
            product = preorder_line.product_id
            moves = moves_by_product.get(product.id, self.env["stock.move"])
            if not moves:
                raise UserError(_("No stock move was created for %s.") % product.display_name)
            if len(moves) != 1:
                raise UserError(
                    _("The stock route split %s into multiple moves. Complete this delivery from Sales.")
                    % product.display_name
                )
            demand = sum(moves.mapped("product_uom_qty"))
            if not float_is_zero(
                demand - preorder_line.requested_qty,
                precision_rounding=product.uom_id.rounding,
            ):
                raise UserError(_("The generated delivery quantity does not match the pre-order."))

            if product.tracking == "serial":
                moves._do_unreserve()
                lots = lots_by_product[product.id]
                location_ids = self.env["stock.location"].sudo().search(
                    [("id", "child_of", pos_location.id)]
                ).ids
                self.env.cr.execute(
                    "SELECT id FROM stock_quant WHERE lot_id IN %s AND location_id IN %s FOR UPDATE",
                    [tuple(lots.ids), tuple(location_ids)],
                )
                quant_model = self.env["stock.quant"].sudo().with_company(self.company_id)
                unavailable = lots.filtered(
                    lambda lot: float_compare(
                        quant_model._get_available_quantity(
                            product,
                            pos_location,
                            lot_id=lot,
                            strict=False,
                        ),
                        1.0,
                        precision_rounding=product.uom_id.rounding,
                    ) < 0
                )
                if unavailable:
                    raise UserError(
                        _("Serial number(s) are not available in this POS location: %s")
                        % ", ".join(unavailable.mapped("name"))
                    )
                target_move = moves.sorted("id")[:1]
                self.env["stock.move.line"].sudo().create(
                    [
                        {
                            "move_id": target_move.id,
                            "picking_id": target_move.picking_id.id,
                            "company_id": target_move.company_id.id,
                            "product_id": product.id,
                            "product_uom_id": target_move.product_uom.id,
                            "quantity": 1.0,
                            "lot_id": lot.id,
                            "location_id": target_move.location_id.id,
                            "location_dest_id": target_move.location_dest_id.id,
                            "picked": True,
                        }
                        for lot in lots
                    ]
                )
            else:
                reserved = sum(moves.move_line_ids.mapped("quantity"))
                if float_compare(
                    reserved,
                    demand,
                    precision_rounding=product.uom_id.rounding,
                ) < 0:
                    raise UserError(
                        _("There is not enough available stock in this POS location for %s.")
                        % product.display_name
                    )
                moves.move_line_ids.write({"picked": True})

        for picking in pickings.sorted("id"):
            result = picking.with_context(
                skip_backorder=True,
                picking_ids_not_to_backorder=pickings.ids,
            ).button_validate()
            self._ensure_pos_picking_validation_completed(picking, result)
        return pickings

    @api.model
    def _ensure_pos_picking_validation_completed(self, picking, validation_result):
        """Accept post-validation actions only after stock is genuinely done.

        Odoo can return a client action after a successful validation (for
        example, an automatic delivery-slip print). That action is not a stock
        confirmation wizard and must not roll the transaction back. Conversely,
        any action returned while the picking remains open still represents an
        unfinished validation step and is rejected by the POS workflow.
        """
        picking.invalidate_recordset(["state"])
        if picking.state == "done":
            return True
        action_name = ""
        if isinstance(validation_result, dict):
            action_name = validation_result.get("name") or ""
        detail = _(" (%s)") % action_name if action_name else ""
        raise UserError(
            _("The delivery requires an additional stock confirmation%s and was not completed.")
            % detail
        )

    def _pos_delivery_success_payload(self):
        self.ensure_one()
        invoices = self.invoice_ids.filtered(
            lambda move: move.move_type == "out_invoice" and move.state != "cancel"
        ).sorted("id")
        order = self.final_sale_order_id
        return {
            "ok": True,
            "preorder_id": self.id,
            "preorder_name": self.name,
            "sale_order_id": order.id,
            "sale_order_name": order.name,
            "invoice_id": invoices[-1:].id,
            "invoice_name": invoices[-1:].name or "",
            "invoice_report_url": "/report/pdf/account.report_invoice/%s" % invoices[-1:].id if invoices else False,
            "preorder_report_url": "/report/pdf/preorder_management.report_preorder_confirmation/%s" % self.id,
        }

    @api.model
    def finalize_preorder_delivery_pos(
        self, preorder_id, serial_assignments, pos_config_id, idempotency_key, payment_lines=None
    ):
        config, session = self._get_authorized_pos_delivery_context(pos_config_id)
        preorder = self._get_preorder_for_pos(preorder_id, config)
        token = str(idempotency_key or "").strip()
        if not token or len(token) > 128:
            raise UserError(_("The POS fulfillment request identifier is invalid."))

        self.env.cr.execute("SELECT id FROM sale_preorder WHERE id = %s FOR UPDATE", [preorder.id])
        preorder.invalidate_recordset(
            ["state", "final_sale_order_id", "invoice_ids", "pos_fulfillment_token"]
        )
        if preorder.state == "completed":
            if preorder.pos_fulfillment_token == token:
                return preorder._pos_delivery_success_payload()
            raise UserError(_("This pre-order has already been delivered."))
        if preorder.state != "allocated" or preorder.campaign_id.state != "delivery":
            raise UserError(_("This pre-order is no longer ready for delivery."))

        preorder._check_pos_payment_ready()
        if preorder.payment_recording_mode != "delivery":
            # Legacy records retain the original guarded re-date workflow.
            preorder._check_original_payments_redatable(fields.Date.context_today(preorder))
        lots_by_product = preorder._prepare_pos_serial_lots(serial_assignments, config)
        preorder.action_create_delivery_order()
        order = preorder.final_sale_order_id
        order.action_confirm()
        preorder._set_pos_picking_quantities(order, lots_by_product, config)
        if preorder.payment_recording_mode == "delivery":
            if order.invoice_status == "to invoice" or not preorder.invoice_ids:
                invoices = order._create_invoices()
            else:
                invoices = preorder.invoice_ids.filtered(lambda move: move.state != "cancel")
            drafts = invoices.filtered(lambda move: move.state == "draft")
            if drafts:
                drafts.action_post()
            posted = invoices.filtered(lambda move: move.state == "posted")
            if not posted:
                raise UserError(_("No posted customer invoice is available for delivery payment."))
            for invoice in posted:
                preorder._create_pos_delivery_payments(invoice, payment_lines, config)
            preorder._workflow_write({"state": "completed"})
            preorder.message_post(
                body=_("Delivered and invoiced from POS. Original pre-order payment was reversed before delivery; replacement payment was recorded at delivery.")
            )
        else:
            preorder.action_invoice_and_apply_payment()
        preorder.invalidate_recordset(["state", "invoice_ids"])
        if preorder.state != "completed":
            raise UserError(
                _("The original payment did not fully settle the delivery invoice; the POS operation was rolled back.")
            )
        preorder.with_context(allow_preorder_workflow_write=True).write(
            {
                "fulfilled_via": "pos",
                "fulfillment_pos_config_id": config.id,
                "fulfillment_pos_session_id": session.id,
                "fulfillment_user_id": self.env.user.id,
                "fulfilled_at": fields.Datetime.now(),
                "pos_fulfillment_token": token,
            }
        )
        preorder.message_post(
            body=_("Delivered and invoiced from POS %(pos)s by %(user)s.")
            % {"pos": config.display_name, "user": self.env.user.display_name}
        )
        return preorder._pos_delivery_success_payload()


class PosOrderPreorderDelivery(models.Model):
    """Attach a migrated pre-order to the normal POS sale.

    The branch must be able to add accessories or other products before taking
    payment. Therefore the POS order, invoice, stock picking, and POS payment
    records are the accounting documents for the delivery. The pre-order is
    marked complete only after the normal POS pipeline has finished.
    """

    _inherit = "pos.order"

    preorder_id = fields.Many2one(
        "sale.preorder",
        string="Customer Pre-order",
        copy=False,
        readonly=True,
        index=True,
        ondelete="restrict",
    )
    preorder_line_ids = fields.Many2many(
        "sale.preorder.line",
        "pos_order_preorder_line_rel",
        "pos_order_id",
        "preorder_line_id",
        string="Pre-order Lines",
        copy=False,
        readonly=True,
    )

    @api.model
    def _extract_preorder_id(self, value):
        if isinstance(value, dict):
            value = value.get("id")
        elif isinstance(value, (list, tuple)):
            value = value[0] if value else False
        try:
            value = int(value or 0)
        except (TypeError, ValueError):
            return False
        return value or False

    @api.model
    def _extract_preorder_line_ids(self, value):
        if not isinstance(value, (list, tuple)):
            return []
        result = []
        for item in value:
            if isinstance(item, dict):
                item = item.get("id")
            try:
                item = int(item or 0)
            except (TypeError, ValueError):
                continue
            if item and item not in result:
                result.append(item)
        return result

    @api.model
    def _order_fields(self, ui_order):
        payload = ui_order.get("data") if isinstance(ui_order.get("data"), dict) else ui_order
        order_fields = super()._order_fields(ui_order)
        preorder_id = self._extract_preorder_id(payload.get("preorder_id"))
        if not preorder_id:
            return order_fields

        preorder = self.env["sale.preorder"].sudo().browse(preorder_id).exists()
        if not preorder:
            raise UserError(_("The selected pre-order no longer exists."))
        line_ids = self._extract_preorder_line_ids(payload.get("preorder_line_ids"))
        if set(line_ids) != set(preorder.line_ids.ids):
            raise UserError(_("The POS cart does not contain the complete pre-order."))
        order_fields["preorder_id"] = preorder.id
        order_fields["preorder_line_ids"] = [Command.set(line_ids)]
        return order_fields

    def _validate_preorder_cart(self):
        self.ensure_one()
        preorder = self.preorder_id.sudo()
        if not preorder:
            return
        pos_branch = self.branch_id or self.config_id.branch_id
        if preorder.company_id != self.company_id:
            raise UserError(_("The pre-order and POS order belong to different companies."))
        if preorder.branch_id != pos_branch:
            raise UserError(_("The pre-order and POS order belong to different branches."))
        if self.partner_id.commercial_partner_id != preorder.customer_id.commercial_partner_id:
            raise UserError(_("The POS customer must match the pre-order customer."))
        if preorder.payment_recording_mode != "delivery":
            raise UserError(
                _("This pre-order uses the legacy payment workflow and must be delivered from the Pre-order screen.")
            )
        if set(self.preorder_line_ids.ids) != set(preorder.line_ids.ids):
            raise UserError(_("The POS cart does not contain every reserved pre-order line."))

        quantities = defaultdict(float)
        for line in self.lines.filtered(lambda item: item.qty > 0 and not item.refunded_orderline_id):
            quantities[line.product_id.id] += line.qty
        for preorder_line in preorder.line_ids:
            if float_compare(
                quantities[preorder_line.product_id.id],
                preorder_line.requested_qty,
                precision_rounding=preorder_line.product_id.uom_id.rounding,
            ) < 0:
                raise UserError(
                    _("The POS cart is missing the reserved quantity for %s.")
                    % preorder_line.product_id.display_name
                )

    def _complete_preorder_from_pos(self):
        self.ensure_one()
        preorder = self.preorder_id.sudo()
        if not preorder:
            return

        self._validate_preorder_cart()
        self.env.cr.execute(
            "SELECT id FROM sale_preorder WHERE id = %s FOR UPDATE", [preorder.id]
        )
        preorder.invalidate_recordset(
            [
                "state",
                "campaign_id",
                "invoice_ids",
                "fulfillment_pos_order_id",
                "pos_fulfillment_token",
            ]
        )
        if preorder.fulfillment_pos_order_id:
            if preorder.fulfillment_pos_order_id == self:
                return
            raise UserError(_("This pre-order has already been delivered from another POS order."))
        if preorder.state != "allocated" or preorder.campaign_id.state != "delivery":
            raise UserError(_("This pre-order is no longer ready for delivery."))
        confirmations = preorder.payment_confirmation_ids.filtered(lambda item: item.state == "confirmed")
        if not confirmations:
            raise UserError(_("The pre-order payment migration is incomplete; no delivery payment confirmation exists."))

        invoice = self.account_move
        if not invoice:
            self.action_pos_order_invoice()
            self.invalidate_recordset(["account_move"])
            invoice = self.account_move
        if not invoice:
            raise UserError(_("The POS order did not produce a customer invoice."))
        if invoice.state == "draft":
            invoice.sudo().action_post()
        if invoice.state != "posted":
            raise UserError(_("The POS customer invoice could not be posted."))

        invoice.sudo().write({"preorder_id": preorder.id})
        confirmations.write({"state": "consumed"})
        token = self.pos_reference or self.name or "POS-%s" % self.id
        preorder.with_context(allow_preorder_workflow_write=True).write(
            {
                "state": "completed",
                "fulfilled_via": "pos",
                "fulfillment_pos_config_id": self.config_id.id,
                "fulfillment_pos_session_id": self.session_id.id,
                "fulfillment_pos_order_id": self.id,
                "fulfillment_user_id": self.env.user.id,
                "fulfilled_at": fields.Datetime.now(),
                "pos_fulfillment_token": token,
            }
        )
        preorder.message_post(
            body=_(
                "Delivered and invoiced in POS order %(order)s at %(pos)s. "
                "The branch collected the complete POS order payment on the delivery date; "
                "the migrated pre-order payment confirmation is now consumed."
            )
            % {"order": self.display_name, "pos": self.config_id.display_name}
        )

    def _process_order(self, order, existing_order):
        result = super()._process_order(order, existing_order)
        order_id = getattr(result, "id", result)
        pos_order = self.browse(order_id).exists()
        if pos_order.preorder_id:
            pos_order._complete_preorder_from_pos()
        return result

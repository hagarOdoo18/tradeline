# -*- coding: utf-8 -*-

from collections import defaultdict

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError
from odoo.tools import float_compare, float_is_zero


class PosConfig(models.Model):
    _inherit = "pos.config"

    enable_preorder_delivery = fields.Boolean(
        string="Pre-order Delivery",
        default=False,
        help=(
            "Allow this POS to deliver fully paid pre-orders reserved for its branch. "
            "The operation creates the normal sales delivery and invoice and reuses the "
            "original payment; it does not create a POS order or collect another payment."
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


class SalePreorderPosDelivery(models.Model):
    _inherit = "sale.preorder"

    _POS_AUDIT_FIELDS = {
        "fulfilled_via",
        "fulfillment_pos_config_id",
        "fulfillment_pos_session_id",
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
        if session.user_id.id != user.id and not user.has_group("point_of_sale.group_pos_manager"):
            raise AccessError(_("Only the user who opened this POS session can deliver a pre-order."))
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
        }
        if include_lines:
            values["lines"] = [
                {
                    "id": line.id,
                    "product_id": line.product_id.id,
                    "product_name": line.product_id.display_name,
                    "qty": line.requested_qty,
                    "tracking": line.product_id.tracking,
                    "uom_rounding": line.product_id.uom_id.rounding,
                }
                for line in self.line_ids
            ]
        return values

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
        unsupported = preorder.line_ids.filtered(lambda line: line.product_id.tracking == "lot")
        if unsupported:
            raise UserError(
                _("Lot-tracked products are not supported in POS pre-order delivery yet: %s")
                % ", ".join(unsupported.product_id.mapped("display_name"))
            )
        return preorder._serialize_for_pos(include_lines=True)

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
            picking.invalidate_recordset(["state"])
            if result is not True or picking.state != "done":
                raise UserError(
                    _("The delivery requires an additional stock confirmation and was not completed.")
                )
        return pickings

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
        self, preorder_id, serial_assignments, pos_config_id, idempotency_key
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
        lots_by_product = preorder._prepare_pos_serial_lots(serial_assignments, config)
        preorder.action_create_delivery_order()
        order = preorder.final_sale_order_id
        order.action_confirm()
        preorder._set_pos_picking_quantities(order, lots_by_product, config)
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

# -*- coding: utf-8 -*-

from odoo import api, fields, models, _


class PosOrder(models.Model):
    _inherit = "pos.order"

    downpayment_source_quotation_id = fields.Many2one(
        "sale.order",
        string="Downpayment Source Quotation",
        readonly=True,
        copy=False,
    )
    downpayment_source_quotation_name = fields.Char(
        string="Downpayment Source Name",
        readonly=True,
        copy=False,
    )
    downpayment_source_reference_number = fields.Char(
        string="Downpayment Source Reference",
        readonly=True,
        copy=False,
    )

    @api.model
    def _extract_int_id(self, value):
        if isinstance(value, dict):
            value = value.get("id")
        elif isinstance(value, (list, tuple)):
            value = value[0] if value else False
        try:
            parsed = int(value or 0)
        except (TypeError, ValueError):
            return False
        return parsed or False

    @api.model
    def _order_fields(self, ui_order):
        payload = ui_order.get("data") if isinstance(ui_order.get("data"), dict) else ui_order
        order_fields = super()._order_fields(ui_order)
        source_id = self._extract_int_id(
            payload.get("downpayment_quotation_id") or payload.get("downpayment_source_quotation_id")
        )
        source_name = (
            payload.get("downpayment_quotation_name")
            or payload.get("downpayment_source_quotation_name")
            or ""
        ).strip()
        source_reference = (
            payload.get("downpayment_reference_number")
            or payload.get("downpayment_source_reference_number")
            or ""
        ).strip()

        if source_id and "sale.order" in self.env:
            source = self.env["sale.order"].sudo().browse(source_id)
            if source.exists():
                order_fields["downpayment_source_quotation_id"] = source.id
                order_fields["downpayment_source_quotation_name"] = source.name or source_name
                if "reference_number" in source._fields:
                    order_fields["downpayment_source_reference_number"] = source.reference_number or source.name
                else:
                    order_fields["downpayment_source_reference_number"] = source.name
                return order_fields

        if source_name:
            order_fields["downpayment_source_quotation_name"] = source_name
            if source_name.lower().startswith("manual ref"):
                manual_ref = source_name.split(":", 1)[1].strip() if ":" in source_name else ""
                order_fields["downpayment_source_reference_number"] = manual_ref or source_name
            elif source_reference:
                order_fields["downpayment_source_reference_number"] = source_reference
        elif source_reference:
            order_fields["downpayment_source_reference_number"] = source_reference

        return order_fields

    def _create_invoice(self, move_vals):
        self.ensure_one()
        move = super()._create_invoice(move_vals)
        if not move or not move.exists() or "reference_number" not in move._fields:
            return move

        quotation_reference = False
        if self.downpayment_source_quotation_id:
            source = self.downpayment_source_quotation_id
            if "reference_number" in source._fields and source.reference_number:
                quotation_reference = source.reference_number
            else:
                quotation_reference = source.name
        elif self.downpayment_source_reference_number:
            quotation_reference = self.downpayment_source_reference_number
        elif self.downpayment_source_quotation_name:
            quotation_reference = self.downpayment_source_quotation_name

        if not quotation_reference:
            return move

        current_reference = (move.reference_number or "").strip()
        invoice_origin = (move.invoice_origin or "").strip()
        tracking_number = (self.tracking_number or "").strip() if "tracking_number" in self._fields else ""
        if not current_reference or current_reference in {invoice_origin, tracking_number}:
            move.reference_number = quotation_reference
        return move

    @api.model
    def _get_allowed_downpayment_inv_types(self):
        return ("quotation", "invoice")

    @api.model
    def _is_downpayment_quotation_line(self, line):
        if not line or line.display_type:
            return False

        if "is_downpayment" in line._fields and line.is_downpayment:
            return True

        line_text = " ".join(
            value for value in [
                line.name or "",
                line.product_id.display_name if line.product_id else "",
            ] if value
        ).lower()
        return "down payment" in line_text or "downpayment" in line_text

    @api.model
    def _has_downpayment_text_signal_pos(self, source_order):
        for line in source_order.order_line:
            line_text = " ".join(
                value for value in [
                    line.name or "",
                    line.product_id.display_name if line.product_id else "",
                ] if value
            ).lower()
            if "down payment" in line_text or "downpayment" in line_text:
                return True
        return False

    @api.model
    def _resolve_pos_branch_id(self, branch_id=False, pos_config_id=False):
        resolved_branch_id = False
        try:
            resolved_branch_id = int(branch_id or 0)
        except (TypeError, ValueError):
            resolved_branch_id = False
        if resolved_branch_id:
            return resolved_branch_id

        try:
            config_id = int(pos_config_id or 0)
        except (TypeError, ValueError):
            config_id = False
        if config_id and "pos.config" in self.env:
            config = self.env["pos.config"].sudo().browse(config_id)
            if config.exists() and "branch_id" in config._fields and config.branch_id:
                return config.branch_id.id

        if getattr(self.env.user, "branch_id", False):
            return self.env.user.branch_id.id
        return False

    @api.model
    def _build_downpayment_source_domain_pos(
        self,
        search_text=False,
        source_inv_type=False,
        enforce_branch=True,
        enforce_validity=True,
        branch_id=False,
        pos_config_id=False,
    ):
        if "sale.order" not in self.env:
            return []

        sale_order_model = self.env["sale.order"].sudo()
        domain = []
        if "state" in sale_order_model._fields:
            domain.append(("state", "in", ["draft", "sent", "sale"]))

        if "company_id" in sale_order_model._fields:
            domain.append(("company_id", "=", self.env.company.id))

        if "inv_type" in sale_order_model._fields:
            domain.append(("inv_type", "=", "quotation"))
        if "invoice_status" in sale_order_model._fields:
            domain.append(("invoice_status", "=", "no"))
        domain += [
            "|", "|", "|",
            ("order_line.product_id.name", "ilike", "down payment"),
            ("order_line.product_id.name", "ilike", "downpayment"),
            ("order_line.name", "ilike", "down payment"),
            ("order_line.name", "ilike", "downpayment"),
        ]

        if enforce_branch and "branch_id" in sale_order_model._fields:
            effective_branch_id = self._resolve_pos_branch_id(
                branch_id=branch_id,
                pos_config_id=pos_config_id,
            )
            if effective_branch_id:
                domain.append(("branch_id", "=", effective_branch_id))

        search_text = (search_text or "").strip()
        if search_text:
            fields_to_search = []
            for field_name in ("reference_number", "name", "client_order_ref", "barcode"):
                if field_name in sale_order_model._fields:
                    fields_to_search.append((field_name, "ilike", search_text))
            if "partner_id" in sale_order_model._fields:
                fields_to_search.append(("partner_id.name", "ilike", search_text))

            if fields_to_search:
                if len(fields_to_search) == 1:
                    domain.append(fields_to_search[0])
                else:
                    domain += ["|"] * (len(fields_to_search) - 1) + fields_to_search

        return domain

    @api.model
    def _is_valid_downpayment_source_pos(self, source_order, source_inv_type=False):
        source_type = source_order.inv_type if "inv_type" in source_order._fields else False
        if source_inv_type and source_inv_type != "quotation":
            return False
        if source_type != "quotation":
            return False
        if "state" in source_order._fields and source_order.state not in ("draft", "sent", "sale"):
            return False
        if "invoice_status" in source_order._fields and source_order.invoice_status != "no":
            return False

        has_real_lines = any(
            self._is_downpayment_quotation_line(line)
            for line in source_order.order_line
        )
        if has_real_lines:
            return True
        return self._has_downpayment_text_signal_pos(source_order)

    @api.model
    def _search_downpayment_source_by_reference_pos(
        self,
        reference_text,
        source_inv_type=False,
        enforce_branch=False,
        enforce_validity=False,
        branch_id=False,
        pos_config_id=False,
    ):
        if "sale.order" not in self.env:
            return self.env["sale.order"]

        ref = (reference_text or "").strip()
        if not ref:
            return self.env["sale.order"]

        sale_order_model = self.env["sale.order"].sudo()
        base_domain = self._build_downpayment_source_domain_pos(
            source_inv_type=source_inv_type,
            enforce_branch=enforce_branch,
            enforce_validity=enforce_validity,
            branch_id=branch_id,
            pos_config_id=pos_config_id,
        )

        for field_name in ("reference_number", "name", "client_order_ref", "barcode"):
            if field_name not in sale_order_model._fields:
                continue
            match = sale_order_model.search(base_domain + [(field_name, "=", ref)], limit=1)
            if match:
                return match

        return sale_order_model.search(
            self._build_downpayment_source_domain_pos(
                search_text=ref,
                source_inv_type=source_inv_type,
                enforce_branch=enforce_branch,
                enforce_validity=enforce_validity,
                branch_id=branch_id,
                pos_config_id=pos_config_id,
            ),
            order="write_date desc, id desc",
            limit=1,
        )

    @api.model
    def _prepare_downpayment_source_payload_pos(self, source_order):
        source_order.ensure_one()
        lines = source_order.order_line.filtered(
            lambda line: self._is_downpayment_quotation_line(line)
        )

        payload_lines = []
        missing_in_pos = []
        for line in lines:
            if not line.product_id:
                continue

            if "available_in_pos" in line.product_id._fields and not line.product_id.available_in_pos:
                missing_in_pos.append(line.product_id.display_name)
                continue

            qty = line.product_uom_qty or 0.0
            if qty <= 0:
                qty = 1.0

            payload_lines.append({
                "product_id": line.product_id.id,
                "product_name": line.product_id.display_name,
                "qty": qty,
                "price_unit": line.price_unit or 0.0,
                "discount": line.discount or 0.0,
            })

        return {
            "source_id": source_order.id,
            "source_name": source_order.name,
            "source_inv_type": source_order.inv_type if "inv_type" in source_order._fields else False,
            "source_branch_name": source_order.branch_id.display_name if "branch_id" in source_order._fields and source_order.branch_id else "",
            "reference_number": source_order.reference_number if "reference_number" in source_order._fields else "",
            "quotation_id": source_order.id,
            "quotation_name": source_order.name,
            "partner_id": source_order.partner_id.id if source_order.partner_id else False,
            "partner_name": source_order.partner_id.display_name or "",
            "validity_date": source_order.validity_date.isoformat() if source_order.validity_date else False,
            "lines": payload_lines,
            "missing_products": missing_in_pos,
        }

    @api.model
    def get_valid_downpayment_quotations_pos(
        self,
        search_text=False,
        limit=100,
        source_inv_type=False,
        branch_id=False,
        pos_config_id=False,
    ):
        if "sale.order" not in self.env:
            return []

        sale_order_model = self.env["sale.order"].sudo()
        try:
            safe_limit = int(limit or 100)
        except (TypeError, ValueError):
            safe_limit = 100
        safe_limit = min(max(safe_limit, 1), 300)

        domain = self._build_downpayment_source_domain_pos(
            search_text=search_text,
            source_inv_type=source_inv_type,
            enforce_branch=True,
            enforce_validity=True,
            branch_id=branch_id,
            pos_config_id=pos_config_id,
        )
        source_orders = sale_order_model.search(domain, order="write_date desc, id desc", limit=safe_limit)
        source_orders = source_orders.filtered(
            lambda order: self._is_valid_downpayment_source_pos(order, source_inv_type=source_inv_type)
        )

        data = []
        for source in source_orders:
            data.append({
                "id": source.id,
                "name": source.name,
                "inv_type": source.inv_type if "inv_type" in source._fields else False,
                "reference_number": source.reference_number if "reference_number" in source._fields else "",
                "partner_name": source.partner_id.display_name or "",
                "partner_id": source.partner_id.id if source.partner_id else False,
                "amount_total": source.amount_total,
                "amount_total_label": "%.2f %s" % (
                    source.amount_total,
                    source.currency_id.name or "",
                ),
                "validity_date": source.validity_date.isoformat() if source.validity_date else False,
                "validity_label": source.validity_date.strftime("%Y-%m-%d") if source.validity_date else _("No Expiration"),
            })
        return data

    @api.model
    def get_downpayment_quotation_details_pos(
        self,
        quotation_id,
        source_inv_type=False,
        allow_cross_branch=False,
        enforce_validity=True,
        branch_id=False,
        pos_config_id=False,
    ):
        if "sale.order" not in self.env:
            return {}

        try:
            quotation_id = int(quotation_id)
        except (TypeError, ValueError):
            return {}

        sale_order_model = self.env["sale.order"].sudo()
        domain = self._build_downpayment_source_domain_pos(
            source_inv_type=source_inv_type,
            enforce_branch=not allow_cross_branch,
            enforce_validity=enforce_validity,
            branch_id=branch_id,
            pos_config_id=pos_config_id,
        )
        domain.append(("id", "=", quotation_id))
        source_order = sale_order_model.search(domain, limit=1)
        if not source_order:
            return {}
        if not self._is_valid_downpayment_source_pos(source_order, source_inv_type=source_inv_type):
            return {}

        return self._prepare_downpayment_source_payload_pos(source_order)

    @api.model
    def get_downpayment_source_by_reference_pos(
        self,
        reference_text,
        source_inv_type=False,
        branch_id=False,
        pos_config_id=False,
    ):
        source_order = self._search_downpayment_source_by_reference_pos(
            reference_text=reference_text,
            source_inv_type=source_inv_type,
            enforce_branch=False,
            enforce_validity=False,
            branch_id=branch_id,
            pos_config_id=pos_config_id,
        )
        if not source_order:
            return {}

        return self._prepare_downpayment_source_payload_pos(source_order)

# -*- coding: utf-8 -*-

from odoo import api, fields, models, _


class PosOrder(models.Model):
    _inherit = "pos.order"

    @api.model
    def _is_downpayment_quotation_line(self, line):
        if not line or line.display_type:
            return False

        if "is_downpayment" in line._fields:
            return bool(line.is_downpayment)

        line_text = " ".join(
            value for value in [
                line.name or "",
                line.product_id.display_name if line.product_id else "",
            ] if value
        ).lower()
        return "down payment" in line_text or "downpayment" in line_text

    @api.model
    def _get_downpayment_quotation_domain_pos(self, search_text=False):
        if "sale.order" not in self.env:
            return []

        sale_order_model = self.env["sale.order"].sudo()
        today = fields.Date.context_today(self)
        domain = [("state", "in", ["draft", "sent"])]

        if "validity_date" in sale_order_model._fields:
            domain += ["|", ("validity_date", "=", False), ("validity_date", ">=", today)]
        if "company_id" in sale_order_model._fields:
            domain.append(("company_id", "=", self.env.company.id))
        if "inv_type" in sale_order_model._fields:
            domain.append(("inv_type", "=", "quotation"))
        if "amount_due" in sale_order_model._fields:
            domain.append(("amount_due", ">", 0))
        if "branch_id" in sale_order_model._fields and getattr(self.env.user, "branch_id", False):
            domain.append(("branch_id", "=", self.env.user.branch_id.id))

        if "sale.order.line" in self.env:
            line_model = self.env["sale.order.line"]
            if "is_downpayment" in line_model._fields:
                domain.append(("order_line.is_downpayment", "=", True))

        if search_text:
            search_text = (search_text or "").strip()
            if search_text:
                domain += [
                    "|",
                    "|",
                    ("name", "ilike", search_text),
                    ("partner_id.name", "ilike", search_text),
                    ("client_order_ref", "ilike", search_text),
                ]
        return domain

    @api.model
    def get_valid_downpayment_quotations_pos(self, search_text=False, limit=100):
        if "sale.order" not in self.env:
            return []

        sale_order_model = self.env["sale.order"].sudo()
        try:
            safe_limit = int(limit or 100)
        except (TypeError, ValueError):
            safe_limit = 100
        safe_limit = min(max(safe_limit, 1), 300)
        domain = self._get_downpayment_quotation_domain_pos(search_text=search_text)
        quotations = sale_order_model.search(domain, order="write_date desc, id desc", limit=safe_limit)

        has_is_downpayment = (
            "sale.order.line" in self.env
            and "is_downpayment" in self.env["sale.order.line"]._fields
        )
        if not has_is_downpayment:
            quotations = quotations.filtered(
                lambda order: any(
                    self._is_downpayment_quotation_line(line)
                    for line in order.order_line
                )
            )

        data = []
        for quotation in quotations:
            data.append({
                "id": quotation.id,
                "name": quotation.name,
                "partner_name": quotation.partner_id.display_name or "",
                "partner_id": quotation.partner_id.id if quotation.partner_id else False,
                "amount_total": quotation.amount_total,
                "amount_total_label": "%.2f %s" % (
                    quotation.amount_total,
                    quotation.currency_id.name or "",
                ),
                "validity_date": quotation.validity_date.isoformat() if quotation.validity_date else False,
                "validity_label": quotation.validity_date.strftime("%Y-%m-%d") if quotation.validity_date else _("No Expiration"),
            })
        return data

    @api.model
    def get_downpayment_quotation_details_pos(self, quotation_id):
        if "sale.order" not in self.env:
            return {}

        try:
            quotation_id = int(quotation_id)
        except (TypeError, ValueError):
            return {}

        sale_order_model = self.env["sale.order"]
        domain = self._get_downpayment_quotation_domain_pos()
        domain.append(("id", "=", quotation_id))
        quotation = sale_order_model.search(domain, limit=1)
        if not quotation:
            return {}

        lines = quotation.order_line.filtered(
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
            "quotation_id": quotation.id,
            "quotation_name": quotation.name,
            "partner_id": quotation.partner_id.id if quotation.partner_id else False,
            "partner_name": quotation.partner_id.display_name or "",
            "validity_date": quotation.validity_date.isoformat() if quotation.validity_date else False,
            "lines": payload_lines,
            "missing_products": missing_in_pos,
        }

# -*- coding: utf-8 -*-
from collections import defaultdict

from odoo import _, api, models


class ProductTemplate(models.Model):
    _inherit = "product.template"

    @api.model
    def get_pos_configurator_availability(self, product_tmpl_id, pos_config_id, qty=1):
        result = {
            "hide_line_ids": [],
            "allowed_value_ids_by_line": {},
            "vendor_value_by_value_id": {},
            "default_vendor_value_id": False,
            "is_blocked": False,
            "message": "",
        }

        try:
            min_qty = float(qty or 1.0)
        except (TypeError, ValueError):
            min_qty = 1.0
        min_qty = max(min_qty, 0.0)

        product_tmpl = self.browse(product_tmpl_id).exists()
        pos_config = self.env["pos.config"].browse(pos_config_id).exists()
        if not product_tmpl or not pos_config:
            return result

        pos_location = pos_config.picking_type_id.default_location_src_id
        if not pos_location:
            return result

        attribute_lines = product_tmpl.attribute_line_ids
        vendor_lines = self._get_vendor_attribute_lines(attribute_lines)
        non_vendor_lines = attribute_lines - vendor_lines
        vendor_line_ids = set(vendor_lines.ids)

        result["hide_line_ids"] = vendor_lines.ids

        variants = product_tmpl.product_variant_ids
        if not variants:
            result.update(
                {
                    "is_blocked": True,
                    "message": _(
                        "No saleable variants are available for this product in %(location)s."
                    )
                    % {"location": pos_location.display_name},
                }
            )
            return result

        quant_domain = [
            ("product_id", "in", variants.ids),
            ("location_id", "child_of", pos_location.id),
            ("location_id.usage", "=", "internal"),
        ]
        if pos_config.company_id:
            quant_domain.append(("company_id", "in", [False, pos_config.company_id.id]))

        quant_env = self.env["stock.quant"].sudo().with_company(pos_config.company_id or self.env.company)
        quants = quant_env.search(quant_domain)

        available_qty_by_variant = defaultdict(float)
        for quant in quants:
            available_qty_by_variant[quant.product_id.id] += quant.quantity - quant.reserved_quantity

        allowed_value_ids_by_line = defaultdict(set)
        vendor_candidate_by_value = {}
        default_vendor_candidate = None
        saleable_variant_count = 0

        for variant in variants:
            available_qty = available_qty_by_variant.get(variant.id, 0.0)
            if available_qty <= 0 or available_qty < min_qty:
                continue

            saleable_variant_count += 1
            ptavs = variant.product_template_variant_value_ids
            vendor_ptavs = ptavs.filtered(lambda ptav: ptav.attribute_line_id.id in vendor_line_ids)
            selected_vendor_id = vendor_ptavs[:1].id if vendor_ptavs else False

            candidate = {
                "vendor_value_id": selected_vendor_id,
                "available_qty": available_qty,
                "variant_id": variant.id,
            }

            if selected_vendor_id and self._is_better_vendor_candidate(candidate, default_vendor_candidate):
                default_vendor_candidate = candidate

            non_vendor_ptavs = ptavs.filtered(lambda ptav: ptav.attribute_line_id.id not in vendor_line_ids)
            for ptav in non_vendor_ptavs:
                allowed_value_ids_by_line[ptav.attribute_line_id.id].add(ptav.id)
                if not selected_vendor_id:
                    continue
                current = vendor_candidate_by_value.get(ptav.id)
                if self._is_better_vendor_candidate(candidate, current):
                    vendor_candidate_by_value[ptav.id] = candidate

        result["allowed_value_ids_by_line"] = {
            line.id: sorted(allowed_value_ids_by_line.get(line.id, set())) for line in non_vendor_lines
        }
        result["vendor_value_by_value_id"] = {
            value_id: data["vendor_value_id"] for value_id, data in vendor_candidate_by_value.items()
        }
        result["default_vendor_value_id"] = (
            default_vendor_candidate["vendor_value_id"] if default_vendor_candidate else False
        )

        if saleable_variant_count == 0:
            result.update(
                {
                    "is_blocked": True,
                    "message": _("This product is out of stock in %(location)s.")
                    % {"location": pos_location.display_name},
                }
            )
            return result

        line_without_values = next(
            (line for line in non_vendor_lines if not result["allowed_value_ids_by_line"].get(line.id)),
            False,
        )
        if line_without_values:
            result.update(
                {
                    "is_blocked": True,
                    "message": _(
                        "No available %(attribute)s options remain in %(location)s."
                    )
                    % {
                        "attribute": line_without_values.attribute_id.display_name,
                        "location": pos_location.display_name,
                    },
                }
            )
            return result

        if vendor_lines and not result["default_vendor_value_id"]:
            result.update(
                {
                    "is_blocked": True,
                    "message": _(
                        "No in-stock vendor combination is available in %(location)s."
                    )
                    % {"location": pos_location.display_name},
                }
            )

        return result

    @api.model
    def _get_vendor_attribute_lines(self, attribute_lines):
        attribute_model = self.env["product.attribute"]
        has_vendor_flag = "is_vendor" in attribute_model._fields

        if has_vendor_flag:
            vendor_lines = attribute_lines.filtered(lambda line: line.attribute_id.is_vendor)
            if vendor_lines:
                return vendor_lines

        return attribute_lines.filtered(
            lambda line: (line.attribute_id.name or "").strip().lower() == "vendor"
        )

    @api.model
    def _is_better_vendor_candidate(self, candidate, current):
        if not candidate or not candidate.get("vendor_value_id"):
            return False
        if not current:
            return True
        if candidate["available_qty"] > current["available_qty"]:
            return True
        if candidate["available_qty"] < current["available_qty"]:
            return False
        return candidate["variant_id"] < current["variant_id"]

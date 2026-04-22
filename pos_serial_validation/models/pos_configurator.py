# -*- coding: utf-8 -*-
from collections import defaultdict

from odoo import _, api, models


class ProductTemplate(models.Model):
    _inherit = "product.template"

    @api.model
    def get_pos_configurator_availability(self, product_tmpl_id, pos_config_id, qty=1):
        is_tracked_product = False
        result = {
            "hide_line_ids": [],
            "allowed_value_ids_by_line": {},
            "vendor_value_by_value_id": {},
            "variant_value_by_value_id": {},
            "default_vendor_value_id": False,
            "default_attribute_value_ids": [],
            "default_variant_attribute_value_ids": [],
            "variant_line_ids": [],
            "variant_attribute_ids": [],
            "is_tracked_product": is_tracked_product,
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
        is_tracked_product = product_tmpl.tracking in ("serial", "lot")
        result["is_tracked_product"] = is_tracked_product

        pos_location = pos_config.picking_type_id.default_location_src_id
        if not pos_location:
            return result

        attribute_lines = product_tmpl.attribute_line_ids
        vendor_lines = self._get_vendor_attribute_lines(attribute_lines)
        vendor_line_ids = set(vendor_lines.ids)

        # Vendor is hidden in POS configurator for both tracked and non-tracked products.
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
        result["variant_line_ids"] = variants.mapped("product_template_variant_value_ids.attribute_line_id").ids
        result["variant_attribute_ids"] = variants.mapped("product_template_variant_value_ids.attribute_id").ids

        display_value_by_attribute_value = {}
        display_line_id_by_value_id = {}
        display_values_by_attribute = defaultdict(list)
        ptav_model = self.env["product.template.attribute.value"]
        has_ptav_active = "ptav_active" in ptav_model._fields
        for line in attribute_lines:
            line_values = line.product_template_value_ids
            display_values = line_values
            if has_ptav_active:
                active_values = line_values.filtered(lambda value: value.ptav_active)
                if active_values:
                    display_values = active_values
            for value in display_values:
                display_line_id_by_value_id[value.id] = line.id
                key = (line.attribute_id.id, value.product_attribute_value_id.id)
                display_value_by_attribute_value.setdefault(key, value.id)
                display_values_by_attribute[line.attribute_id.id].append(
                    {
                        "id": value.id,
                        "normalized_name": self._normalize_attribute_value_name(
                            value.product_attribute_value_id.name or value.name or ""
                        ),
                        "digits": self._extract_digits(value.product_attribute_value_id.name or value.name or ""),
                    }
                )

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
        variant_value_candidate_by_value = {}
        default_vendor_candidate = None
        default_variant_candidate = None
        participating_line_ids = set()
        saleable_variant_count = 0

        for variant in variants:
            available_qty = available_qty_by_variant.get(variant.id, 0.0)
            if available_qty <= 0 or available_qty < min_qty:
                continue

            saleable_variant_count += 1
            ptavs = variant.product_template_variant_value_ids
            participating_line_ids.update(ptavs.mapped("attribute_line_id").ids)
            display_ptav_ids = set()
            canonical_ptav_ids = set(ptavs.ids)
            for ptav in ptavs:
                display_ptav_id = self._resolve_display_ptav_id(
                    ptav, display_value_by_attribute_value, display_values_by_attribute
                )
                display_ptav_ids.add(display_ptav_id)

            variant_candidate = {
                "available_qty": available_qty,
                "variant_id": variant.id,
                "attribute_value_ids": sorted(canonical_ptav_ids),
                "display_attribute_value_ids": sorted(display_ptav_ids),
            }
            if self._is_better_variant_candidate(variant_candidate, default_variant_candidate):
                default_variant_candidate = variant_candidate

            for display_ptav_id in display_ptav_ids:
                line_id = display_line_id_by_value_id.get(display_ptav_id)
                if line_id:
                    allowed_value_ids_by_line[line_id].add(display_ptav_id)

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
                display_ptav_id = self._resolve_display_ptav_id(
                    ptav, display_value_by_attribute_value, display_values_by_attribute
                )
                value_alias_candidate = {
                    "variant_value_id": ptav.id,
                    "available_qty": available_qty,
                    "variant_id": variant.id,
                }
                for key in {ptav.id, display_ptav_id}:
                    existing_alias = variant_value_candidate_by_value.get(key)
                    if self._is_better_value_alias_candidate(value_alias_candidate, existing_alias):
                        variant_value_candidate_by_value[key] = value_alias_candidate

                if not selected_vendor_id:
                    continue
                for key in {ptav.id, display_ptav_id}:
                    current = vendor_candidate_by_value.get(key)
                    if self._is_better_vendor_candidate(candidate, current):
                        vendor_candidate_by_value[key] = candidate

        result["allowed_value_ids_by_line"] = {
            line.id: sorted(allowed_value_ids_by_line.get(line.id, set())) for line in attribute_lines
        }
        result["vendor_value_by_value_id"] = {
            value_id: data["vendor_value_id"] for value_id, data in vendor_candidate_by_value.items()
        }
        result["variant_value_by_value_id"] = {
            value_id: data["variant_value_id"] for value_id, data in variant_value_candidate_by_value.items()
        }
        result["default_vendor_value_id"] = (
            default_vendor_candidate["vendor_value_id"] if default_vendor_candidate else False
        )
        result["default_attribute_value_ids"] = (
            default_variant_candidate["display_attribute_value_ids"] if default_variant_candidate else []
        )
        result["default_variant_attribute_value_ids"] = (
            default_variant_candidate["attribute_value_ids"] if default_variant_candidate else []
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

        # Block only when a line that is part of in-stock variants has no selectable values.
        required_lines = attribute_lines.filtered(lambda line: line.id in participating_line_ids)
        required_lines -= vendor_lines

        line_without_values = next((line for line in required_lines if not allowed_value_ids_by_line.get(line.id)), False)
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

        # Do not block sale when vendor is hidden but unavailable in variant payload.
        # This happens on templates where vendor is informational/non-variant.
        if vendor_lines and not result["default_vendor_value_id"]:
            result["message"] = ""

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

    @api.model
    def _is_better_variant_candidate(self, candidate, current):
        if not candidate:
            return False
        if not current:
            return True
        if candidate["available_qty"] > current["available_qty"]:
            return True
        if candidate["available_qty"] < current["available_qty"]:
            return False
        return candidate["variant_id"] < current["variant_id"]

    @api.model
    def _is_better_value_alias_candidate(self, candidate, current):
        if not candidate or not candidate.get("variant_value_id"):
            return False
        if not current:
            return True
        if candidate["available_qty"] > current["available_qty"]:
            return True
        if candidate["available_qty"] < current["available_qty"]:
            return False
        return candidate["variant_id"] < current["variant_id"]

    @api.model
    def _resolve_display_ptav_id(
        self,
        ptav,
        display_value_by_attribute_value,
        display_values_by_attribute,
    ):
        if not ptav:
            return False

        attribute = ptav.attribute_id or ptav.attribute_line_id.attribute_id
        key = (attribute.id, ptav.product_attribute_value_id.id)
        exact_match = display_value_by_attribute_value.get(key)
        if exact_match:
            return exact_match

        candidates = display_values_by_attribute.get(attribute.id, [])
        if not candidates:
            return ptav.id

        source_names = [
            ptav.product_attribute_value_id.name or "",
            ptav.name or "",
        ]
        normalized_tokens = [
            self._normalize_attribute_value_name(name) for name in source_names if name
        ]
        digit_tokens = [self._extract_digits(name) for name in source_names if name]
        digit_tokens = [digits for digits in digit_tokens if digits]

        for token in normalized_tokens:
            if not token:
                continue
            candidate = next((item for item in candidates if item["normalized_name"] == token), False)
            if candidate:
                return candidate["id"]

        for digits in digit_tokens:
            candidate = next((item for item in candidates if item["digits"] == digits), False)
            if candidate:
                return candidate["id"]

        for token in normalized_tokens:
            if not token:
                continue
            candidate = next(
                (
                    item
                    for item in candidates
                    if item["normalized_name"] and (token in item["normalized_name"] or item["normalized_name"] in token)
                ),
                False,
            )
            if candidate:
                return candidate["id"]

        return ptav.id

    @api.model
    def _normalize_attribute_value_name(self, name):
        return "".join(ch for ch in (name or "").lower() if ch.isalnum())

    @api.model
    def _extract_digits(self, name):
        return "".join(ch for ch in (name or "") if ch.isdigit())

# -*- coding: utf-8 -*-
from collections import defaultdict

from odoo import _, api, models


class ProductTemplate(models.Model):
    _inherit = "product.template"

    @api.model
    def _init_pos_availability_payload(self):
        return {
            "hide_line_ids": [],
            "allowed_value_ids_by_line": {},
            "vendor_value_by_value_id": {},
            "variant_value_by_value_id": {},
            "default_vendor_value_id": False,
            "default_attribute_value_ids": [],
            "default_variant_attribute_value_ids": [],
            "variant_line_ids": [],
            "variant_attribute_ids": [],
            "is_tracked_product": False,
            "is_blocked": False,
            "message": "",
            "payload_version": 2,
            "stock_decision": "ok",  # ok | true_oos | inconsistent
            "consistency_status": "ok",
            "warning_code": False,
            "warning_message": "",
            "auto_add_default": False,
        }

    @api.model
    def get_pos_configurator_availability(self, product_tmpl_id, pos_config_id, qty=1):
        result = self._init_pos_availability_payload()

        try:
            min_qty = float(qty or 1.0)
        except (TypeError, ValueError):
            min_qty = 1.0
        min_qty = max(min_qty, 0.0)

        product_tmpl = self.browse(product_tmpl_id).exists()
        pos_config = self.env["pos.config"].browse(pos_config_id).exists()
        if not product_tmpl or not pos_config:
            self._set_inconsistent_warning(
                result,
                "missing_context",
                _("Stock checks are temporarily incomplete. Please verify availability before payment."),
            )
            return result

        result["is_tracked_product"] = product_tmpl.tracking in ("serial", "lot")
        pos_location = self._get_pos_source_location(pos_config)
        if not pos_location:
            self._set_inconsistent_warning(
                result,
                "missing_pos_source_location",
                _(
                    "POS source location is not configured for %(pos)s. Please verify stock before payment."
                )
                % {"pos": pos_config.display_name},
            )
            return result

        attribute_lines = product_tmpl.attribute_line_ids
        vendor_lines = self._get_vendor_attribute_lines(attribute_lines)
        vendor_line_ids = set(vendor_lines.ids)
        result["hide_line_ids"] = vendor_lines.ids

        variants = product_tmpl.product_variant_ids
        if not variants:
            return self._set_true_oos(
                result,
                _("No saleable variants are available for this product in %(location)s.")
                % {"location": pos_location.display_name},
            )

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
                        "digits": self._extract_digits(
                            value.product_attribute_value_id.name or value.name or ""
                        ),
                    }
                )

        available_qty_by_variant = self._get_pos_available_qty_by_product_ids(
            variants.ids,
            pos_config,
            pos_location,
        )

        allowed_value_ids_by_line = defaultdict(set)
        vendor_candidate_by_value = {}
        variant_value_candidate_by_value = {}
        default_vendor_candidate = None
        default_variant_candidate = None
        participating_line_ids = set()
        saleable_variant_count = 0
        positive_stock_variant_count = 0

        for variant in variants:
            available_qty = available_qty_by_variant.get(variant.id, 0.0)
            if available_qty > 0:
                positive_stock_variant_count += 1
            if available_qty <= 0 or available_qty < min_qty:
                continue

            saleable_variant_count += 1
            ptavs = variant.product_template_variant_value_ids
            participating_line_ids.update(ptavs.mapped("attribute_line_id").ids)
            display_ptav_ids = set()
            canonical_ptav_ids = set(ptavs.ids)

            for ptav in ptavs:
                display_ptav_id = self._resolve_display_ptav_id(
                    ptav,
                    display_value_by_attribute_value,
                    display_values_by_attribute,
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
                    ptav,
                    display_value_by_attribute_value,
                    display_values_by_attribute,
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
            str(line.id): sorted(allowed_value_ids_by_line.get(line.id, set()))
            for line in attribute_lines
        }
        result["vendor_value_by_value_id"] = {
            str(value_id): data["vendor_value_id"]
            for value_id, data in vendor_candidate_by_value.items()
        }
        result["variant_value_by_value_id"] = {
            str(value_id): data["variant_value_id"]
            for value_id, data in variant_value_candidate_by_value.items()
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
            if min_qty > 1 and positive_stock_variant_count:
                return self._set_true_oos(
                    result,
                    _(
                        "Requested quantity (%(qty)s) exceeds available stock in %(location)s."
                    )
                    % {
                        "qty": min_qty,
                        "location": pos_location.display_name,
                    },
                )
            return self._set_true_oos(
                result,
                _("This product is out of stock in %(location)s.")
                % {"location": pos_location.display_name},
            )

        if not default_variant_candidate:
            self._set_inconsistent_warning(
                result,
                "missing_default_variant",
                _(
                    "Some stock mappings could not be resolved for %(product)s. Please verify before payment."
                )
                % {"product": product_tmpl.display_name},
            )

        required_lines = attribute_lines.filtered(lambda line: line.id in participating_line_ids)
        required_lines -= vendor_lines

        line_without_values = next(
            (line for line in required_lines if not allowed_value_ids_by_line.get(line.id)),
            False,
        )
        if line_without_values:
            self._set_inconsistent_warning(
                result,
                "required_line_unmapped",
                _(
                    "Some %(attribute)s options could not be validated in %(location)s. Please verify before payment."
                )
                % {
                    "attribute": line_without_values.attribute_id.display_name,
                    "location": pos_location.display_name,
                },
            )

        if vendor_lines and not result["default_vendor_value_id"]:
            self._set_inconsistent_warning(
                result,
                "vendor_unmapped",
                _("Vendor could not be auto-mapped for %(product)s. Please verify before payment.")
                % {"product": product_tmpl.display_name},
            )

        if result["consistency_status"] == "ok":
            visible_variant_lines = required_lines.filtered(lambda line: line.id not in vendor_line_ids)
            has_user_choice = any(
                len(allowed_value_ids_by_line.get(line.id, set())) > 1 for line in visible_variant_lines
            )
            result["auto_add_default"] = bool(
                default_variant_candidate
                and not result["is_tracked_product"]
                and not has_user_choice
            )
        else:
            result["auto_add_default"] = False

        return result

    @api.model
    def get_pos_available_qty_by_products(self, pos_config_id, product_ids):
        pos_config = self.env["pos.config"].browse(pos_config_id).exists()
        if not pos_config:
            return {}
        qty_by_product = self._get_pos_available_qty_by_product_ids(product_ids, pos_config)
        return {str(product_id): qty for product_id, qty in qty_by_product.items()}

    @api.model
    def _get_pos_source_location(self, pos_config):
        if not pos_config:
            return False
        return pos_config.picking_type_id.default_location_src_id or False

    @api.model
    def _build_pos_quant_domain(
        self,
        product_ids,
        pos_config,
        pos_location=None,
        extra_domain=None,
        include_company=True,
    ):
        product_ids = [int(product_id) for product_id in (product_ids or []) if product_id]
        if not product_ids:
            return []

        pos_location = pos_location or self._get_pos_source_location(pos_config)
        if not pos_location:
            return []

        domain = [
            ("product_id", "in", product_ids),
            ("location_id", "child_of", pos_location.id),
            ("location_id.usage", "=", "internal"),
        ]
        if include_company:
            company_ids = [False]
            if pos_config and pos_config.company_id:
                company_ids.append(pos_config.company_id.id)
            if pos_location.company_id:
                company_ids.append(pos_location.company_id.id)
            company_ids = list(dict.fromkeys(company_ids))
            if len(company_ids) > 1:
                domain.append(("company_id", "in", company_ids))
        if extra_domain:
            domain.extend(extra_domain)
        return domain

    @api.model
    def _get_pos_available_qty_by_product_ids(
        self,
        product_ids,
        pos_config,
        pos_location=None,
        extra_domain=None,
        include_company=True,
    ):
        domain = self._build_pos_quant_domain(
            product_ids,
            pos_config,
            pos_location=pos_location,
            extra_domain=extra_domain,
            include_company=include_company,
        )
        if not domain:
            return {}

        quant_env = self.env["stock.quant"].sudo().with_company(pos_config.company_id or self.env.company)
        qty_by_product = defaultdict(float)
        for quant in quant_env.search(domain):
            qty_by_product[quant.product_id.id] += quant.quantity - quant.reserved_quantity
        return dict(qty_by_product)

    @api.model
    def _set_true_oos(self, result, message):
        result.update(
            {
                "stock_decision": "true_oos",
                "is_blocked": True,
                "message": message,
                "consistency_status": "ok",
                "warning_code": False,
                "warning_message": "",
                "auto_add_default": False,
            }
        )
        return result

    @api.model
    def _set_inconsistent_warning(self, result, warning_code, warning_message):
        if result.get("consistency_status") == "inconsistent":
            return result
        result.update(
            {
                "stock_decision": "inconsistent",
                "is_blocked": False,
                "message": "",
                "consistency_status": "inconsistent",
                "warning_code": warning_code,
                "warning_message": warning_message,
                "auto_add_default": False,
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
                    if item["normalized_name"]
                    and (token in item["normalized_name"] or item["normalized_name"] in token)
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

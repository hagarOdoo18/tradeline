from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class DiscountReason(models.Model):
    _inherit = "discount.reason"

    use_category_discount = fields.Boolean(
        string="Use Category-Based Discounts",
        tracking=True,
        help=(
            "If enabled, POS will apply line discounts by product category using the "
            "configuration table below."
        ),
    )
    category_discount_line_ids = fields.One2many(
        comodel_name="discount.reason.category.line",
        inverse_name="discount_reason_id",
        string="Category Discount Rules",
        copy=True,
    )

    @api.constrains("use_category_discount", "category_discount_line_ids", "discount_type")
    def _check_category_discount_configuration(self):
        for reason in self:
            has_rules = bool(
                reason.category_discount_line_ids.filtered(lambda line: line.category_ids)
            )
            if reason.use_category_discount and not has_rules:
                raise ValidationError(
                    _(
                        "Category-based discount reasons require at least one category rule."
                    )
                )
            if not reason.use_category_discount and reason.category_discount_line_ids:
                raise ValidationError(
                    _(
                        "Remove category rules or enable 'Use Category-Based Discounts'."
                    )
                )

    @api.model
    def _load_pos_data_fields(self, config_id):
        fields_list = super()._load_pos_data_fields(config_id)
        if "use_category_discount" not in fields_list:
            fields_list.append("use_category_discount")
        if "discount_type" not in fields_list:
            fields_list.append("discount_type")
        if "fixed_discount_amount" not in fields_list:
            fields_list.append("fixed_discount_amount")
        return fields_list


class DiscountReasonCategoryLine(models.Model):
    _name = "discount.reason.category.line"
    _description = "Discount Reason Category Rule"
    _order = "sequence, id"

    discount_reason_id = fields.Many2one(
        comodel_name="discount.reason",
        string="Discount Reason",
        required=True,
        ondelete="cascade",
        index=True,
    )
    sequence = fields.Integer(default=10)
    category_ids = fields.Many2many(
        comodel_name="product.category",
        relation="discount_reason_category_line_categ_rel",
        column1="line_id",
        column2="categ_id",
        string="Product Categories",
        required=True,
    )
    family_ids = fields.Many2many(
        comodel_name="product.family",
        relation="discount_reason_category_line_family_rel",
        column1="line_id",
        column2="family_id",
        string="Families",
        help=(
            "Optional. If set, this rule only applies when the product family matches "
            "one of the selected families."
        ),
    )
    discount_percentage = fields.Float(
        string="Discount Percentage (%)",
        required=True,
    )

    _sql_constraints = [
        (
            "discount_reason_category_line_percentage_range",
            "CHECK(discount_percentage >= 0 AND discount_percentage <= 100)",
            "Discount percentage must be between 0 and 100.",
        ),
    ]

    @api.constrains("category_ids")
    def _check_has_category(self):
        for line in self:
            if not line.category_ids:
                raise ValidationError(_("Please select at least one product category."))

    @api.constrains("category_ids", "family_ids")
    def _check_family_scope_category_count(self):
        for line in self:
            if line.family_ids and len(line.category_ids) != 1:
                raise ValidationError(
                    _(
                        "When families are set on a rule, select exactly one product category."
                    )
                )

    @api.constrains("discount_reason_id", "category_ids", "family_ids")
    def _check_duplicate_category_family_rules(self):
        category_model = self.env["product.category"]
        family_model = self.env["product.family"]

        for reason in self.mapped("discount_reason_id"):
            seen_keys = {}
            for line in reason.category_discount_line_ids:
                category_ids = line.category_ids.ids
                if not category_ids:
                    continue

                family_ids = line.family_ids.ids
                if family_ids:
                    if len(category_ids) != 1:
                        continue
                    category_id = category_ids[0]
                    for family_id in family_ids:
                        key = (category_id, family_id)
                        if key in seen_keys:
                            category_name = category_model.browse(category_id).display_name
                            family_name = family_model.browse(family_id).display_name
                            raise ValidationError(
                                _(
                                    "Duplicate rule for category '%(category)s' and "
                                    "family '%(family)s' in discount reason '%(reason)s'."
                                )
                                % {
                                    "category": category_name,
                                    "family": family_name,
                                    "reason": reason.display_name,
                                }
                            )
                        seen_keys[key] = line.id
                    continue

                for category_id in category_ids:
                    key = (category_id, False)
                    if key in seen_keys:
                        category_name = category_model.browse(category_id).display_name
                        raise ValidationError(
                            _(
                                "Duplicate fallback category rule for category '%(category)s' "
                                "in discount reason '%(reason)s'."
                            )
                            % {
                                "category": category_name,
                                "reason": reason.display_name,
                            }
                        )
                    seen_keys[key] = line.id

    @api.model
    def _load_pos_data_domain(self, data):
        reason_data = data.get("discount.reason", {}).get("data", [])
        reason_ids = [reason.get("id") for reason in reason_data if reason.get("id")]
        if reason_ids:
            return [("discount_reason_id", "in", reason_ids)]
        return []

    @api.model
    def _load_pos_data_fields(self, config_id):
        return [
            "id",
            "discount_reason_id",
            "category_ids",
            "family_ids",
            "discount_percentage",
            "sequence",
        ]

    @api.model
    def _load_pos_data(self, data):
        domain = self._load_pos_data_domain(data)
        fields = self._load_pos_data_fields(data["pos.config"]["data"][0]["id"])
        rules = self.search_read(domain, fields, load=False)
        return {
            "data": rules,
            "fields": fields,
        }

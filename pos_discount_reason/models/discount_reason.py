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

    @api.model
    def _load_pos_data_fields(self, config_id):
        fields_list = super()._load_pos_data_fields(config_id)
        if "use_category_discount" not in fields_list:
            fields_list.append("use_category_discount")
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

    @api.constrains("discount_percentage", "discount_reason_id")
    def _check_max_percentage(self):
        for line in self:
            if (
                line.discount_reason_id
                and line.discount_percentage > line.discount_reason_id.discount_percentage
            ):
                raise ValidationError(
                    _(
                        "Category discount percentage cannot exceed the discount reason "
                        "maximum percentage."
                    )
                )

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
            "discount_percentage",
            "sequence",
        ]

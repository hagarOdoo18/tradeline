# models/pos_order.py
from odoo import models, fields, api, _
from odoo.exceptions import UserError
from odoo.tools.float_utils import float_compare


class PosOrder(models.Model):
    _inherit = 'pos.order'


    discount_reason_id = fields.Many2one(
        'discount.reason',
        string='Discount Reason',
        help='Reason for applying discount on this order'
    )

    as_gift = fields.Boolean(
        string='As Gift',
        default=False,
        help='Mark this order as a gift'
    )

    sales_rep_id = fields.Many2one(
        'sales.rep',
        string='Sales Representative',
        required=True,
        help='Employee who made this sale'
    )

    @api.model
    def _order_fields(self, ui_order):
        """Add discount_reason, as_gift and sales_rep_id to order fields"""
        order_fields = super(PosOrder, self)._order_fields(ui_order)

        # Add discount reason from UI order
        if ui_order.get('discount_reason_id'):
            order_fields['discount_reason_id'] = ui_order['discount_reason_id']

        # Add as_gift from UI order
        if ui_order.get('as_gift'):
            order_fields['as_gift'] = ui_order['as_gift']

        sales_rep_id = ui_order.get('sales_rep_id')
        if isinstance(sales_rep_id, dict):
            sales_rep_id = sales_rep_id.get('id')
        if not sales_rep_id:
            raise UserError(_("Sales Representative is required before validating the order."))
        order_fields['sales_rep_id'] = sales_rep_id

        return order_fields

    @staticmethod
    def _extract_m2o_id(value):
        if isinstance(value, dict):
            return value.get('id')
        if isinstance(value, (list, tuple)):
            return value[0] if value else False
        return value

    @staticmethod
    def _extract_line_vals(line_command):
        if isinstance(line_command, (list, tuple)) and len(line_command) == 3:
            return line_command[2] or {}
        if isinstance(line_command, dict):
            return line_command
        return {}

    def _get_category_chain_ids(self, category):
        chain = []
        seen = set()
        current = category
        while current and current.id not in seen:
            chain.append(current.id)
            seen.add(current.id)
            current = current.parent_id
        return chain

    def _get_reason_category_cap_for_product(self, reason, product):
        rules = reason.category_discount_line_ids.filtered(lambda l: l.category_ids)
        if not reason.use_category_discount or not rules:
            return None

        category_chain = self._get_category_chain_ids(product.categ_id)
        if not category_chain:
            return None

        product_family = getattr(product.product_tmpl_id, "family_id", False)
        product_family_id = product_family.id if product_family else False

        best_match = None
        for rule in rules.sorted(key=lambda r: (r.sequence, r.id)):
            for category in rule.category_ids:
                if category.id not in category_chain:
                    continue

                has_family_scope = bool(rule.family_ids)
                if has_family_scope and (
                    not product_family_id or product_family_id not in rule.family_ids.ids
                ):
                    continue

                depth = category_chain.index(category.id)
                specificity = 0 if has_family_scope else 1
                if not best_match or specificity < best_match[0] or (
                    specificity == best_match[0] and depth < best_match[1]
                ) or (
                    specificity == best_match[0]
                    and depth == best_match[1]
                    and rule.sequence < best_match[2]
                ):
                    best_match = (
                        specificity,
                        depth,
                        rule.sequence,
                        rule.discount_percentage,
                    )

        return best_match[3] if best_match else None

    def _get_reason_category_names(self, reason):
        category_names = reason.category_discount_line_ids.mapped('category_ids').mapped('display_name')
        return ", ".join(sorted(set(category_names))) if category_names else _("No categories configured")

    def _get_reason_scope_display(self, reason):
        display_parts = []
        for rule in reason.category_discount_line_ids.sorted(key=lambda r: (r.sequence, r.id)):
            category_names = ", ".join(rule.category_ids.mapped("display_name"))
            if rule.family_ids:
                family_names = ", ".join(rule.family_ids.mapped("display_name"))
                display_parts.append(_("%(categories)s [Families: %(families)s]") % {
                    "categories": category_names,
                    "families": family_names,
                })
            elif category_names:
                display_parts.append(category_names)

        return "; ".join(display_parts) if display_parts else _("No category rules configured")

    def _validate_locked_category_discounts(self, order_payload):
        order_vals = order_payload.get('data') if isinstance(order_payload, dict) and order_payload.get('data') else order_payload
        if not isinstance(order_vals, dict):
            return

        reason_id = self._extract_m2o_id(order_vals.get('discount_reason_id'))
        if not reason_id:
            return

        reason = self.env['discount.reason'].browse(reason_id).exists()
        if not reason:
            return

        has_category_rules = bool(reason.category_discount_line_ids.filtered(lambda l: l.category_ids))
        if reason.use_category_discount and not has_category_rules:
            raise UserError(
                _(
                    "Discount reason '%s' is configured for category discounts but has no category rules."
                ) % reason.name
            )

        eligible_line_count = 0
        for line_cmd in order_vals.get('lines', []):
            line_vals = self._extract_line_vals(line_cmd)
            product_id = self._extract_m2o_id(line_vals.get('product_id'))
            if not product_id:
                continue

            product = self.env['product.product'].browse(product_id).exists()
            if not product:
                continue

            actual_discount = float(line_vals.get('discount') or 0.0)

            if reason.use_category_discount:
                category_cap = self._get_reason_category_cap_for_product(reason, product)
                if category_cap is None:
                    if float_compare(actual_discount, 0.0, precision_digits=2) == 1:
                        raise UserError(
                            _(
                                "Product '%(product)s' is not eligible for discount reason '%(reason)s'. "
                                "Allowed scope: %(scope)s."
                            ) % {
                                'product': product.display_name,
                                'reason': reason.name,
                                'scope': self._get_reason_scope_display(reason),
                            }
                        )
                    continue

                eligible_line_count += 1
                if float_compare(actual_discount, category_cap, precision_digits=2) == 1:
                    raise UserError(
                        _(
                            "Discount for product '%(product)s' cannot exceed %(cap).2f%% "
                            "for reason '%(reason)s'."
                        ) % {
                            'product': product.display_name,
                            'cap': category_cap,
                            'reason': reason.name,
                    }
                )
                continue

            reason_cap = reason.discount_percentage or 0.0
            if float_compare(actual_discount, reason_cap, precision_digits=2) == 1:
                raise UserError(
                    _(
                        "Discount for product '%(product)s' cannot exceed %(cap).2f%% "
                        "for reason '%(reason)s'."
                    ) % {
                        'product': product.display_name,
                        'cap': reason_cap,
                        'reason': reason.name,
                    }
                )

        if reason.use_category_discount and has_category_rules and not eligible_line_count:
            raise UserError(
                _(
                    "No order lines are eligible for discount reason '%(reason)s'. "
                    "Remove the discount reason or add products from allowed scope: %(scope)s."
                ) % {
                    'reason': reason.name,
                    'scope': self._get_reason_scope_display(reason),
                }
            )

    def _process_order(self, order, *args):
        self._validate_locked_category_discounts(order)
        return super()._process_order(order, *args)

    def _prepare_order_line(self, line):
        """Prepare order line data"""
        line_data = super(PosOrder, self)._prepare_order_line(line)
        return line_data

    def _create_invoice(self, move_vals):
        """Override to auto print invoice after creation"""
        move = super()._create_invoice(move_vals)
        move.sales_rep_id = self.sales_rep_id.id
        move.discount_id = self.discount_reason_id.id
        move.order_number = self.tracking_number

        for line  in  move.invoice_line_ids :
            warranty = self.env['product.warranty'].search([('categ_ids', 'in', line.product_id.categ_id.id)])

            line.family_id = line.product_id.product_tmpl_id.family_id.id
            line.categ_id = line.product_id.categ_id.id
            line.product_upc = line.product_id.default_code
            line.item_code = line.product_id.barcode
            line.vendor_id = line.product_id.vendor_id
            line.standard_price = line.product_id.standard_price
            if move.move_type =='out_refund' :
                line.product_point = line.product_id.product_point *line.quantity *-1
            else:
                line.product_point = line.product_id.product_point *line.quantity

            line.warranty_id = warranty if warranty else False

        # Auto print the invoice after creation

        return move

    @api.model
    def sync_from_ui(self, orders):
        data = super().sync_from_ui(orders)
        return data

    @api.model
    def get_discount_reason_rules_pos(self, reason_id):
        reason = self.env['discount.reason'].sudo().browse(reason_id).exists()
        if not reason or not reason.use_category_discount:
            return []

        lines = reason.category_discount_line_ids.sudo().filtered(
            lambda l: l.category_ids
        ).sorted(key=lambda l: (l.sequence, l.id))

        return [
            {
                'sequence': line.sequence or 10,
                'discount_percentage': line.discount_percentage or 0.0,
                'category_ids': line.category_ids.ids,
                'family_ids': line.family_ids.ids,
            }
            for line in lines
        ]

    @api.model
    def get_products_family_map_pos(self, product_ids):
        if not product_ids:
            return {}

        normalized_ids = []
        for pid in product_ids:
            try:
                normalized_ids.append(int(pid))
            except (TypeError, ValueError):
                continue

        if not normalized_ids:
            return {}

        products = self.env['product.product'].sudo().browse(normalized_ids).exists()
        result = {}
        for product in products:
            family = False
            if 'family_id' in product._fields:
                family = product.family_id
            elif 'family_id' in product.product_tmpl_id._fields:
                family = product.product_tmpl_id.family_id
            result[product.id] = family.id if family else False
        return result





class PosConfig(models.Model):
    _inherit = 'pos.config'

    auto_invoice = fields.Boolean(
        string='Auto Invoice',
        default=True,
        help='Automatically create invoices for all POS orders'
    )

    auto_print_invoice = fields.Boolean(
        string='Auto Print Invoice',
        default=True,
        help='Automatically print invoices after creation'
    )


class PosSession(models.Model):
    _inherit = 'pos.session'


    def _loader_params_product_product(self):
        params = super()._loader_params_product_product()
        fields_list = params.get('search_params', {}).get('fields', [])
        if 'family_id' not in fields_list:
            fields_list.append('family_id')
        return params


    def _loader_params_pos_config(self):
        """Load pos.config fields"""
        params = super()._loader_params_pos_config()
        params['search_params']['fields'].extend(['auto_invoice', 'auto_print_invoice'])
        return params


    def _load_pos_data(self, data):
        """Load POS data and add `res_users` to the response dictionary.
        return: A dictionary containing the POS data.
        """
        data = super()._load_pos_data(data)
        data['data'][0]['sales_reps'] = self.env['sales.rep'].search_read(fields=['id', 'name'])
        data['data'][0]['discount_reason'] = self.env['discount.reason'].search_read(
            fields=['id', 'name', 'discount_percentage', 'use_category_discount']
        )
        data['data'][0]['discount_reason_category_lines'] = self.env['discount.reason.category.line'].search_read(
            fields=['id', 'discount_reason_id', 'category_ids', 'family_ids', 'discount_percentage', 'sequence']
        )
        return data

    @api.model
    def _load_pos_data_models(self, config_id):
        data = super()._load_pos_data_models(config_id)
        for model_name in ['sales.rep', 'discount.reason', 'discount.reason.category.line']:
            if model_name not in data:
                data.append(model_name)
        return data

from odoo import fields, models, api, _
from random import randint
from odoo.exceptions import UserError
from odoo.tools.float_utils import float_compare, float_is_zero

from odoo.http import Controller, request, route
from odoo.exceptions import ValidationError
class SaleOrder(models.Model):
    _inherit = 'sale.order'



    reference_number = fields.Char(
        string='Reference Number',
        required=True)
    state = fields.Selection(selection_add=[('to_use', 'To Use'), ('refund', 'Refund')])

    def action_set_to_use(self):
        self.write({
            'state': 'to_use'
        })

    def action_set_to_refund(self):
        self.write({
            'state': 'refund'
        })

    def get_product_notes(self):
            for rec in self.order_line:
                if rec.product_id.product_notes:
                    return rec.product_id.product_notes
                else:
                    return ''

    def generate_barcode(self):
        val = 0
        for res in self:
            i = True
            while i:
                barcode = ''.join(["%s" % randint(0, 9) for num in range(0, 13)])
                sale_order = self.search([('barcode', '=', barcode)])
                if len(sale_order) <= 0:
                    # _logger.info("barcode with generate %s", barcode)
                    res.barcode = barcode
                    val = barcode
                    i = False

    barcode = fields.Char(
        string='Barcode',
        required=False)

    @api.model_create_multi
    def create(self, vals_list):
        res = super(SaleOrder, self).create(vals_list)
        if not res.barcode:
            res.generate_barcode()
        return res

    discount_id = fields.Many2one(
        comodel_name='discount.reason',
        string='Discount Reason',
        required=False)

    channel_id = fields.Many2one(
        comodel_name='channel.channel',
        string='Channel',
        required=False)

    courier_id = fields.Many2one(
        comodel_name='courier.courier',
        string='Courier',
        required=False)

    bank_id = fields.Many2one(
        comodel_name='bank.details',
        string='Bank',
        required=False)


    sales_rep_id = fields.Many2one(
        comodel_name='sales.rep',
        string='Sales Rep',
        required=True)

    inv_type = fields.Selection(
        string='Invoice Type',default='invoice',
        selection=[('sro', 'SRO'),('quotation','Quotation'),
                   ('invoice', 'Invoice'), ('debit', 'Debit')],
        required=True, )
    downpayment_source_quotation_id = fields.Many2one(
        comodel_name='sale.order',
        string='Downpayment Source',
        copy=False,
        help='Select a valid downpayment source for this branch and invoice type.',
    )
    manual_downpayment_reference = fields.Char(
        string='Manual Downpayment',
        copy=False,
        help='Enter any downpayment reference manually, then click Load Downpayment.',
    )
    sales_rep_domain = fields.Char(
        string='Sales_rep_domain',
        required=False)
    discount_domain = fields.Char(
        string='Sales_rep_domain',
        required=False)

    product_notes = fields.Char(
        string='Product Notes',
        required=False)

    @api.onchange('order_line')
    def onchange_order_line_product_note(self):
        for line in self.order_line:
            if line.product_id.product_notes != '':
                self.product_notes == line.product_id.product_notes
    def action_draft(self):
        return self.write({
            'state': 'draft',
            'signature': False,
            'signed_by': False,
            'signed_on': False,
        })

    @api.onchange('branch_id')
    def onchange_branch_id(self):
        if self.branch_id:
            self.sales_rep_domain = "['|',('branch_id','=',"+str(self.branch_id.id)+"),('branch_id','=',False)]"
            self.discount_domain = "[('branches_ids','in',"+str(self.branch_id.id)+"),('state','=','run')]"

        else:
            self.sales_rep_domain="[('branch_id','=',0)]"
            self.discount_domain="[('branches_ids','=',0)]"

    @api.model
    def _is_downpayment_quotation_line(self, line):
        if not line or line.display_type:
            return False
        if "is_downpayment" in line._fields and line.is_downpayment:
            return True

        line_text = " ".join([
            line.name or "",
            line.product_id.display_name if line.product_id else "",
        ]).lower()
        return "down payment" in line_text or "downpayment" in line_text

    def _has_downpayment_product_lines(self):
        self.ensure_one()
        return bool(
            self.order_line.filtered(
                lambda line: line.product_id and self._is_downpayment_quotation_line(line)
            )
        )

    @api.onchange("inv_type")
    def _onchange_inv_type_validate_downpayment(self):
        for order in self:
            if order.inv_type != "quotation" and order._has_downpayment_product_lines():
                raise UserError(_("Down Payment product can only be used when Invoice Type is Quotation."))

    @api.constrains("inv_type", "order_line", "order_line.product_id", "order_line.name", "order_line.display_type")
    def _check_inv_type_validate_downpayment(self):
        for order in self:
            if order.inv_type != "quotation" and order._has_downpayment_product_lines():
                raise ValidationError(_("Down Payment product can only be used when Invoice Type is Quotation."))

    def _get_allowed_downpayment_inv_types(self):
        return ("quotation", "invoice")

    def _build_downpayment_source_domain(
        self,
        source_inv_type=None,
        enforce_branch=True,
        enforce_validity=True,
        reference_text=False,
    ):
        self.ensure_one()
        sale_order_model = self.env["sale.order"]
        domain = [("id", "!=", self.id)]
        if "state" in sale_order_model._fields:
            domain.append(("state", "in", ["draft", "sent", "sale"]))

        if "company_id" in sale_order_model._fields:
            domain.append(("company_id", "=", self.company_id.id))

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

        if enforce_branch and "branch_id" in sale_order_model._fields and self.branch_id:
            domain.append(("branch_id", "=", self.branch_id.id))

        ref = (reference_text or "").strip()
        if ref:
            ref_domain = []
            if "reference_number" in sale_order_model._fields:
                ref_domain.append(("reference_number", "ilike", ref))
            if "name" in sale_order_model._fields:
                ref_domain.append(("name", "ilike", ref))
            if "client_order_ref" in sale_order_model._fields:
                ref_domain.append(("client_order_ref", "ilike", ref))
            if "barcode" in sale_order_model._fields:
                ref_domain.append(("barcode", "ilike", ref))
            if ref_domain:
                if len(ref_domain) == 1:
                    domain.append(ref_domain[0])
                else:
                    domain += ["|"] * (len(ref_domain) - 1) + ref_domain
        return domain

    def _find_downpayment_source_by_reference(
        self,
        reference_text,
        source_inv_type=None,
        enforce_branch=False,
        enforce_validity=False,
    ):
        self.ensure_one()
        ref = (reference_text or "").strip()
        if not ref:
            return self.env["sale.order"]

        sale_order_model = self.env["sale.order"].sudo()
        base_domain = self._build_downpayment_source_domain(
            source_inv_type=source_inv_type,
            enforce_branch=enforce_branch,
            enforce_validity=enforce_validity,
        )
        fields_to_try = ["reference_number", "name", "client_order_ref", "barcode"]
        for field_name in fields_to_try:
            if field_name not in sale_order_model._fields:
                continue
            exact_match = sale_order_model.search(base_domain + [(field_name, "=", ref)], limit=1)
            if exact_match:
                return exact_match

        return sale_order_model.search(
            self._build_downpayment_source_domain(
                source_inv_type=source_inv_type,
                enforce_branch=enforce_branch,
                enforce_validity=enforce_validity,
                reference_text=ref,
            ),
            order="write_date desc, id desc",
            limit=1,
        )

    def _get_valid_downpayment_lines(
        self,
        source_quotation,
        enforce_branch=True,
        enforce_validity=True,
        allow_empty=False,
    ):
        self.ensure_one()
        source_quotation.ensure_one()

        if source_quotation.id == self.id:
            raise UserError(_("You cannot load downpayment lines from the same document."))

        if "state" in source_quotation._fields and source_quotation.state not in ("draft", "sent", "sale"):
            raise UserError(_("Selected source document must be in Quotation or Sales Order status."))

        if "inv_type" in source_quotation._fields and source_quotation.inv_type != "quotation":
            raise UserError(_("Selected source document must be a quotation."))
        if "invoice_status" in source_quotation._fields and source_quotation.invoice_status != "no":
            raise UserError(_("Selected source quotation must be in 'Nothing to Invoice' status."))

        if source_quotation.company_id != self.company_id:
            raise UserError(_("Selected source document belongs to another company."))

        if enforce_branch and "branch_id" in source_quotation._fields and "branch_id" in self._fields:
            if self.branch_id and source_quotation.branch_id and source_quotation.branch_id != self.branch_id:
                raise UserError(_("Selected source document belongs to another branch."))

        downpayment_lines = source_quotation.order_line.filtered(
            lambda line: self._is_downpayment_quotation_line(line) and line.product_id
        )
        if downpayment_lines:
            return downpayment_lines

        if allow_empty:
            return self.env["sale.order.line"]
        raise UserError(_("Selected source document has no downpayment lines to load."))

    def _prepare_downpayment_line_commands(self, source_lines):
        self.ensure_one()
        line_model = self.env["sale.order.line"]
        has_tax_id = "tax_id" in line_model._fields
        has_tax_ids = "tax_ids" in line_model._fields

        line_commands = []
        for line in source_lines:
            qty = line.product_uom_qty if (line.product_uom_qty or 0.0) > 0 else 1.0
            vals = {
                "name": line.name or line.product_id.display_name,
                "product_id": line.product_id.id,
                "product_uom_qty": qty,
                "price_unit": line.price_unit or 0.0,
                "discount": line.discount or 0.0,
            }

            if "product_uom" in line_model._fields and line.product_uom:
                vals["product_uom"] = line.product_uom.id

            tax_records = self.env["account.tax"]
            if "tax_id" in line._fields:
                tax_records = line.tax_id
            elif "tax_ids" in line._fields:
                tax_records = line.tax_ids

            if has_tax_id:
                vals["tax_id"] = [(6, 0, tax_records.ids)]
            elif has_tax_ids:
                vals["tax_ids"] = [(6, 0, tax_records.ids)]

            line_commands.append((0, 0, vals))

        return line_commands

    def _prepare_downpayment_fill_vals(
        self,
        source,
        enforce_branch=True,
        enforce_validity=True,
        allow_empty_lines=False,
    ):
        self.ensure_one()
        source_lines = self._get_valid_downpayment_lines(
            source,
            enforce_branch=enforce_branch,
            enforce_validity=enforce_validity,
            allow_empty=allow_empty_lines,
        )
        vals = {
            "partner_id": source.partner_id.id if source.partner_id else self.partner_id.id,
            "pricelist_id": source.pricelist_id.id if source.pricelist_id else self.pricelist_id.id,
            "downpayment_source_quotation_id": source.id,
        }
        if source_lines:
            vals["order_line"] = self._prepare_downpayment_line_commands(source_lines)
        if "team_id" in self._fields and source.team_id:
            vals["team_id"] = source.team_id.id
        if "sales_rep_id" in self._fields and "sales_rep_id" in source._fields and source.sales_rep_id:
            vals["sales_rep_id"] = source.sales_rep_id.id
        if "reference_number" in self._fields:
            vals["reference_number"] = source.reference_number or source.name
        return vals

    @api.onchange("downpayment_source_quotation_id")
    def _onchange_downpayment_source_quotation_id(self):
        for order in self:
            source = order.downpayment_source_quotation_id
            if not source:
                continue

            if order.inv_type not in order._get_allowed_downpayment_inv_types():
                return {
                    "warning": {
                        "title": _("Invalid Invoice Type"),
                        "message": _("Load Downpayment is only allowed when Invoice Type is Quotation or Invoice."),
                    }
                }

            existing_lines = order.order_line.filtered(lambda line: not line.display_type)
            if existing_lines:
                return {
                    "warning": {
                        "title": _("Quotation Not Empty"),
                        "message": _("Please use an empty quotation before loading downpayment lines."),
                    }
                }

            try:
                vals = order._prepare_downpayment_fill_vals(
                    source.sudo(),
                    enforce_branch=True,
                    enforce_validity=True,
                    allow_empty_lines=True,
                )
            except UserError as err:
                return {
                    "warning": {
                        "title": _("Cannot Load Downpayment"),
                        "message": str(err),
                    }
                }
            order.update(vals)

    def action_load_downpayment_quotation(self):
        self.ensure_one()
        if self.state != "draft":
            raise UserError(_("You can only load downpayment lines while quotation is in Draft."))

        if self.inv_type not in self._get_allowed_downpayment_inv_types():
            raise UserError(_("Load Downpayment is only allowed when Invoice Type is Quotation or Invoice."))

        existing_lines = self.order_line.filtered(lambda line: not line.display_type)
        if existing_lines:
            raise UserError(_("Please use an empty quotation before loading downpayment lines."))

        if not self.downpayment_source_quotation_id:
            raise UserError(_("Please select Downpayment Source first."))

        source = self.downpayment_source_quotation_id.sudo()
        write_vals = self._prepare_downpayment_fill_vals(
            source,
            enforce_branch=True,
            enforce_validity=True,
            allow_empty_lines=True,
        )

        self.write(write_vals)
        return True

    def _prepare_invoice(self):
        res = super(SaleOrder, self)._prepare_invoice()
        journal = self.env['account.journal'].search([('type','=','sale'),('branch_id','=',self.branch_id.id),('currency_id','=',self.currency_id.id)])
        if not journal and not self.invoice_journal_id:
            raise UserError(('please set Journal for this Branch'))
        res['journal_id'] = journal.id if not self.invoice_journal_id else self.invoice_journal_id.id
        res['opportunity_id'] = self.opportunity_id.id
        res['discount_id'] = self.discount_id.id
        res['courier_id'] = self.courier_id.id
        res['channel_id'] = self.channel_id.id
        res['product_notes'] = self.product_notes
        res['bank_id'] = self.bank_id.id
        res['sales_rep_id'] = self.sales_rep_id.id
        res['inv_type'] = self.inv_type
        res['reference_number'] = self.reference_number
        res['barcode'] = self.barcode
        res['pricelist_id'] = self.pricelist_id.id

        res['invoice_date'] = fields.Date.today()
        return res

    def action_confirm(self):
        """ Override of `sale` to send the order to Gelato on confirmation. """
        res = super(SaleOrder, self).action_confirm()
        for rec in self:
            if  rec.pricelist_id.currency_id.id != rec.invoice_journal_id.currency_id.id:
                raise UserError("PriceList Not same in journal ")
            if not rec.invoice_journal_id:
                raise UserError("Select Invoice Journal ")

            rec._validate_discount_reason_lines(require_positive_standard=True)

        return res

    def action_view_sale_advance_payment_inv(self):
        if self.filtered(lambda so: so.inv_type == 'sro'):
            raise UserError(_("You cannot create invoices when Invoice Type is SRO."))
        return super(SaleOrder, self).action_view_sale_advance_payment_inv()

    def _reason_uses_category_mode(self, reason):
        return bool(
            reason
            and (reason.discount_type or "percentage") == "percentage"
            and "use_category_discount" in reason._fields
            and reason.use_category_discount
        )

    def _reason_is_fixed_amount_mode(self, reason):
        return bool(reason and (reason.discount_type or "percentage") == "fixed_amount")

    def _reason_has_category_scope(self, reason):
        return bool(
            reason
            and "use_category_discount" in reason._fields
            and reason.use_category_discount
        )

    def _get_discount_reason_line_base_amount(self, line):
        base_amount = (line.price_unit or 0.0) * (line.product_uom_qty or 0.0)
        return max(base_amount, 0.0)

    def _compute_fixed_reason_line_discounts(self, reason, lines):
        self.ensure_one()
        fixed_amount = max(reason.fixed_discount_amount or 0.0, 0.0)
        currency = self.currency_id
        precision_rounding = currency.rounding if currency else 0.01

        has_category_scope = self._reason_has_category_scope(reason)
        if has_category_scope:
            rules = self._get_reason_category_rules(reason)
            if not rules:
                raise UserError(
                    "This discount reason requires category rules but none are configured."
                )

        eligible_lines = []
        for line in lines:
            if has_category_scope and self._get_reason_category_cap_for_product(reason, line.product_id) is None:
                continue
            eligible_lines.append((line, self._get_discount_reason_line_base_amount(line)))
        eligible_lines = [(line, base) for line, base in eligible_lines if base > 0]
        eligible_base_total = sum(base for _, base in eligible_lines)

        if float_compare(fixed_amount, 0.0, precision_rounding=precision_rounding) == 0:
            return {line.id: 0.0 for line in lines}

        if has_category_scope and not eligible_lines:
            raise UserError(
                "No order lines are eligible for this discount reason. "
                "Remove discount reason or add eligible products from allowed scope: %s."
                % self._get_reason_allowed_categories_display(reason)
            )

        if float_compare(eligible_base_total, 0.0, precision_rounding=precision_rounding) <= 0:
            raise UserError("Cannot apply fixed discount on zero-value order lines.")

        if float_compare(fixed_amount, eligible_base_total, precision_rounding=precision_rounding) == 1:
            raise UserError(
                "Fixed discount amount cannot exceed eligible order lines total (%.2f)."
                % eligible_base_total
            )

        percentages = {}
        remaining_amount = fixed_amount
        for index, (line, base_amount) in enumerate(eligible_lines):
            if index == len(eligible_lines) - 1:
                line_discount_amount = remaining_amount
            else:
                line_discount_amount = fixed_amount * (base_amount / eligible_base_total)
                remaining_amount -= line_discount_amount

            discount_percentage = (line_discount_amount / base_amount) * 100 if base_amount else 0.0
            percentages[line.id] = min(max(discount_percentage, 0.0), 100.0)

        for line in lines - self.env["sale.order.line"].browse(list(percentages.keys())):
            percentages[line.id] = 0.0

        return percentages

    def _get_order_lines_discount_amount_total(self, lines):
        total = 0.0
        for line in lines:
            base_amount = self._get_discount_reason_line_base_amount(line)
            if base_amount <= 0:
                continue
            total += base_amount * ((line.discount or 0.0) / 100.0)
        return total

    def _get_reason_category_rules(self, reason):
        if not reason or "category_discount_line_ids" not in reason._fields:
            return self.env["discount.reason.category.line"]
        return reason.category_discount_line_ids.filtered(lambda r: r.category_ids)

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
        rules = self._get_reason_category_rules(reason)
        if not self._reason_has_category_scope(reason) or not rules:
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

    def _get_reason_allowed_categories_display(self, reason):
        display_parts = []
        for rule in self._get_reason_category_rules(reason).sorted(key=lambda r: (r.sequence, r.id)):
            category_names = ", ".join(rule.category_ids.mapped("display_name"))
            if rule.family_ids:
                family_names = ", ".join(rule.family_ids.mapped("display_name"))
                display_parts.append(
                    "%s [Families: %s]" % (category_names, family_names)
                )
            elif category_names:
                display_parts.append(category_names)
        return "; ".join(display_parts) if display_parts else "No category rules configured"

    def _apply_discount_reason_to_lines(self):
        for order in self:
            reason = order.discount_id
            if not reason:
                continue

            lines = order.order_line.filtered("product_id")
            if not lines:
                continue

            if order._reason_is_fixed_amount_mode(reason):
                fixed_percentages = order._compute_fixed_reason_line_discounts(reason, lines)
                for line in lines:
                    line.discount = fixed_percentages.get(line.id, 0.0)
                continue

            if not order._reason_uses_category_mode(reason):
                for line in lines:
                    line.discount = reason.discount_percentage or 0.0
                continue

            rules = order._get_reason_category_rules(reason)
            if not rules:
                raise UserError(
                    "This discount reason requires category rules but none are configured."
                )

            eligible_lines = 0
            for line in lines:
                category_cap = order._get_reason_category_cap_for_product(reason, line.product_id)
                if category_cap is None:
                    line.discount = 0.0
                    continue
                line.discount = category_cap
                eligible_lines += 1

            if not eligible_lines:
                raise UserError(
                    "No order lines are eligible for this discount reason. "
                    "Remove discount reason or add eligible products from allowed scope: %s."
                    % order._get_reason_allowed_categories_display(reason)
                )

    def _validate_discount_reason_lines(self, require_positive_standard=False):
        for order in self:
            reason = order.discount_id
            if not reason:
                continue

            lines = order.order_line.filtered("product_id")
            if not lines:
                continue

            if order._reason_is_fixed_amount_mode(reason):
                fixed_amount = max(reason.fixed_discount_amount or 0.0, 0.0)
                precision_rounding = order.currency_id.rounding if order.currency_id else 0.01
                has_category_scope = order._reason_has_category_scope(reason)
                if has_category_scope and not order._get_reason_category_rules(reason):
                    raise UserError(
                        "This discount reason requires category rules but none are configured."
                    )

                eligible_count = 0
                for line in lines:
                    if has_category_scope:
                        category_cap = order._get_reason_category_cap_for_product(reason, line.product_id)
                        if category_cap is None:
                            if float_compare(line.discount or 0.0, 0.0, precision_digits=2) == 1:
                                raise UserError(
                                    "Product '%s' is not eligible for this discount reason. Allowed scope: %s."
                                    % (
                                        line.product_id.display_name,
                                        order._get_reason_allowed_categories_display(reason),
                                    )
                                )
                            continue
                        eligible_count += 1
                    if float_compare(line.discount or 0.0, 100.0, precision_digits=2) == 1:
                        raise UserError("Discount Not Matched with Discount Reason")

                if has_category_scope and not eligible_count:
                    raise UserError(
                        "No order lines are eligible for this discount reason. "
                        "Remove discount reason or add eligible products from allowed scope: %s."
                        % order._get_reason_allowed_categories_display(reason)
                    )

                discounted_total = order._get_order_lines_discount_amount_total(lines)
                if float_compare(discounted_total, fixed_amount, precision_rounding=precision_rounding) == 1:
                    raise UserError(
                        "Discount Not Matched with Discount Reason. Total line discounts exceed fixed amount %.2f."
                        % fixed_amount
                    )
                if (
                    require_positive_standard
                    and float_compare(fixed_amount, 0.0, precision_rounding=precision_rounding) == 1
                    and float_is_zero(discounted_total, precision_rounding=precision_rounding)
                ):
                    raise UserError("Remove Discount Reason")
                continue

            if not order._reason_uses_category_mode(reason):
                for line in lines:
                    if float_compare(
                        line.discount or 0.0,
                        reason.discount_percentage or 0.0,
                        precision_digits=2,
                    ) == 1:
                        raise UserError("Discount Not Matched with Discount Reason")
                    if (
                        require_positive_standard
                        and float_compare(line.discount or 0.0, 0.0, precision_digits=2) <= 0
                    ):
                        raise UserError("Remove Discount Reason")
                continue

            rules = order._get_reason_category_rules(reason)
            if not rules:
                raise UserError(
                    "This discount reason requires category rules but none are configured."
                )

            eligible_count = 0
            for line in lines:
                discount = line.discount or 0.0
                category_cap = order._get_reason_category_cap_for_product(reason, line.product_id)
                if category_cap is None:
                    if float_compare(discount, 0.0, precision_digits=2) == 1:
                        raise UserError(
                            "Product '%s' is not eligible for this discount reason. Allowed scope: %s."
                            % (
                                line.product_id.display_name,
                                order._get_reason_allowed_categories_display(reason),
                            )
                        )
                    continue

                eligible_count += 1
                if float_compare(discount, category_cap, precision_digits=2) == 1:
                    raise UserError(
                        "Discount for product '%s' cannot exceed %.2f%%."
                        % (line.product_id.display_name, category_cap)
                    )

            if not eligible_count:
                raise UserError(
                    "No order lines are eligible for this discount reason. "
                    "Remove discount reason or add eligible products from allowed scope: %s."
                    % order._get_reason_allowed_categories_display(reason)
                )

    @api.constrains('discount_id', 'order_line', 'order_line.discount', 'order_line.product_id')
    def _check_discount_reason_required_for_discount(self):
        for order in self:
            if order.discount_id:
                continue

            discounted_lines = order.order_line.filtered(
                lambda line: line.product_id and float_compare(line.discount or 0.0, 0.0, precision_digits=2) == 1
            )
            if discounted_lines:
                raise ValidationError(
                    _("Discount Reason is mandatory when any sales order line has a discount.")
                )

    @api.constrains('discount_id', 'order_line', 'order_line.discount', 'order_line.product_id')
    def _check_discount_reason_lines(self):
        self._validate_discount_reason_lines(require_positive_standard=False)

    @api.onchange('discount_id')
    def _onchange_discount_id_apply_reason(self):
        self._apply_discount_reason_to_lines()

    tax_t1 = fields.Float(compute='_compute_tax', string="VAT14%")
    tax_t2 = fields.Float(compute='_compute_tax', string="VAT1%")
    tax_t2_t = fields.Float(compute='_compute_tax', string="VAT2%")
    tax_t3 = fields.Float(compute='_compute_tax', string="VAT3%")
    tax_t5 = fields.Float(compute='_compute_tax', string="VAT5%")
    total = fields.Float(compute='compute_tax', string="Total")

    @api.depends('order_line')
    def _compute_tax(self):
        for rec in self:
            sum_v14 = 0
            sum_v1 = 0
            sum_v3 = 0
            sum_v5 = 0
            sum_v2 = 0

            for line in rec.order_line:
                if line.tax_ids:
                    for tax in line.tax_ids:
                        if tax.name == "14%":
                            sum_v14 += (line.price_subtotal * tax.amount / 100)

                        elif tax.name == "Withholding Tax -1%":
                            sum_v1 += (line.price_subtotal * tax.amount / 100)

                        elif tax.name == "Withholding Tax -3%":
                            sum_v3 += (line.price_subtotal * tax.amount / 100)
                        elif tax.name == "Withholding Tax -5%":
                            sum_v5 += (line.price_subtotal * tax.amount / 100)
                        elif tax.name == "Withholding Tax -2%":
                            sum_v2 += (line.price_subtotal * tax.amount / 100)

            rec.tax_t1 = sum_v14
            rec.tax_t2 = sum_v1
            rec.tax_t3 = sum_v3
            rec.tax_t5 = sum_v5
            rec.tax_t2_t = sum_v2
            rec.total = sum_v14 + rec.amount_untaxed

class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    product_point = fields.Float(
        string='Product point',
        required=False)
    product_incentive = fields.Float(
        string='Product incentive',
        required=False)
    warranty_id = fields.Many2one(
        comodel_name='product.warranty',
        string='Warranty',
        required=False)

    item_code = fields.Char(
        string='Item Code',
        required=False)

    categ_id = fields.Many2one(
        comodel_name='product.category',
        string='Category',
        required=False)

    family_id = fields.Many2one(
        comodel_name='product.family',
        string='Family',
        required=False)

    location_id = fields.Many2one(
        'stock.location',
        string="Stock Location",
        compute='_compute_location_id',
        store=True
    )

    @api.constrains('tax_id')
    def _check_tax_required(self):
        for line in self:
            if line.product_id and line.order_id.inv_type !='sro' and line.product_id.type != 'service'  :
                if not line.tax_id:
                    raise ValidationError(
                        "Tax is required on order line '%s' in quotation '%s'. "
                        "Please set a tax before saving." % (
                            line.name, line.order_id.name
                        )
                    )

    @api.depends('order_id.warehouse_id')
    def _compute_location_id(self):
        for line in self:
            line.location_id = line.order_id.warehouse_id.lot_stock_id


    @api.onchange('product_id')
    def _onchange_product_id_set_values(self):
        self.item_code = self.product_id.default_code
        self.family_id = self.product_id.product_tmpl_id.family_id.id
        self.categ_id = self.product_id.categ_id.id
        warranty =  self.env['product.warranty'].search([('categ_ids','in',self.product_id.categ_id.id)])
        if warranty:
            self.warranty_id =  warranty.id
        if self.order_id and self.order_id.discount_id and self.product_id:
            reason = self.order_id.discount_id
            if self.order_id._reason_is_fixed_amount_mode(reason):
                self.order_id._apply_discount_reason_to_lines()
            elif self.order_id._reason_uses_category_mode(reason):
                category_cap = self.order_id._get_reason_category_cap_for_product(reason, self.product_id)
                self.discount = category_cap if category_cap is not None else 0.0
            else:
                self.discount = reason.discount_percentage or 0.0
        if (
            self.order_id
            and self.order_id.inv_type != "quotation"
            and self.order_id._is_downpayment_quotation_line(self)
        ):
            raise UserError(_("Down Payment product can only be used when Invoice Type is Quotation."))


    def _prepare_invoice_line(self, **optional_values):

        res = super()._prepare_invoice_line(**optional_values)
        warranty = self.env['product.warranty'].search([('categ_ids', 'in', self.product_id.categ_id.id)])

        res['warranty_id'] = warranty.id if warranty else False
        res['product_upc'] =  self.product_id.default_code
        res['item_code'] =  self.product_id.barcode
        res['family_id'] = self.product_id.product_tmpl_id.family_id.id
        res['vendor_id'] = self.product_id.vendor_id.id
        res['standard_price'] = self.product_id.standard_price
        res['product_point'] =  self.product_id.product_point * self.qty_to_invoice
        res['categ_id'] = self.product_id.categ_id.id
        return res

    @api.onchange('discount')
    def _onchange_discount(self):
        for line in self:
            if line.product_id:
                if line.discount != 0 and not line.order_id.discount_id:
                    raise UserError("Select Discount Reason To Apply Discount")
                if not line.order_id.discount_id:
                    continue

                reason = line.order_id.discount_id
                if line.order_id._reason_is_fixed_amount_mode(reason):
                    if line.order_id._reason_has_category_scope(reason):
                        rules = line.order_id._get_reason_category_rules(reason)
                        if not rules:
                            raise UserError("This discount reason requires category rules but none are configured.")
                        category_cap = line.order_id._get_reason_category_cap_for_product(reason, line.product_id)
                        if category_cap is None and float_compare(line.discount or 0.0, 0.0, precision_digits=2) == 1:
                            raise UserError("Product Not Eligible For Selected Discount Reason")
                    if float_compare(line.discount or 0.0, 100.0, precision_digits=2) == 1:
                        raise UserError("Discount Not Matched with Discount Reason")
                    lines = line.order_id.order_line.filtered("product_id")
                    fixed_amount = max(reason.fixed_discount_amount or 0.0, 0.0)
                    precision_rounding = line.order_id.currency_id.rounding if line.order_id.currency_id else 0.01
                    discounted_total = line.order_id._get_order_lines_discount_amount_total(lines)
                    if float_compare(discounted_total, fixed_amount, precision_rounding=precision_rounding) == 1:
                        raise UserError("Discount Not Matched with Discount Reason")
                elif line.order_id._reason_uses_category_mode(reason):
                    rules = line.order_id._get_reason_category_rules(reason)
                    if not rules:
                        raise UserError("This discount reason requires category rules but none are configured.")

                    category_cap = line.order_id._get_reason_category_cap_for_product(reason, line.product_id)
                    if category_cap is None:
                        if float_compare(line.discount or 0.0, 0.0, precision_digits=2) == 1:
                            raise UserError("Product Not Eligible For Selected Discount Reason")
                        continue

                    if float_compare(line.discount or 0.0, category_cap, precision_digits=2) == 1:
                        raise UserError("Discount Not Matched with Discount Reason")
                elif line.discount > line.order_id.discount_id.discount_percentage:
                    raise UserError("Discount Not Matched with Discount Reason")

    @api.onchange('product_id')
    def _onchange_product_id (self):
        if not self.order_id.partner_id:
            raise UserError("Select Customer First")

    lot_id = fields.Many2one(
        "stock.lot",
        "Lot",
        copy=False,
        readonly=False,
    )

    @api.onchange("product_id","qty_delivered")
    def _onchange_lot_id(self):
        for sol in self:

            sol.product_point = sol.product_id.product_tmpl_id.product_point * sol.qty_delivered

    @api.onchange("lot_id")
    def _onchange_lot_id(self):
        for sol in self:
            if sol.lot_id.product_id:
                sol.product_id = sol.lot_id.product_id.id

class saleadvancepaymentinv(models.TransientModel):
    _inherit = 'sale.advance.payment.inv'
    _name = 'sale.advance.payment.inv'

    advance_payment_method = fields.Selection(
        selection=[
            ('delivered', "Regular invoice"),
            ('percentage', "Down payment (percentage)"),
            ('fixed', "Down payment (fixed amount)"),
        ],
        string="Create Invoice",
        default='delivered',
        required=True,
        readonly=True,
        help="A standard invoice is issued with all the order lines ready for invoicing,"
             "according to their invoicing policy (based on ordered or delivered quantity).")

    def _create_invoices(self,sale_orders):
        sro_orders = sale_orders.filtered(lambda so: so.inv_type == 'sro')
        if sro_orders:
            raise UserError(_("You cannot create invoices when Invoice Type is SRO."))

        invoices = super(saleadvancepaymentinv, self)._create_invoices(sale_orders)
        for invoice in invoices:
            invoice.action_post()


        return invoices

# class SaleProductConfiguratorController(Controller):
#
#     def _get_product_information(
#         self,
#         product_template,
#         combination,
#         currency,
#         pricelist,
#         so_date,
#         quantity=1,
#         product_uom_id=None,
#         parent_combination=None,
#         **kwargs,
#     ):
#         """ Return complete information about a product.
#
#         :param product.template product_template: The product for which to seek information.
#         :param product.template.attribute.value combination: The combination of the product.
#         :param res.currency currency: The currency of the transaction.
#         :param product.pricelist pricelist: The pricelist to use.
#         :param datetime so_date: The date of the `sale.order`, to compute the price at the right
#             rate.
#         :param int quantity: The quantity of the product.
#         :param int|None product_uom_id: The unit of measure of the product, as a `uom.uom` id.
#         :param product.template.attribute.value|None parent_combination: The combination of the
#             parent product.
#         :param dict kwargs: Locally unused data passed to `_get_basic_product_information`.
#         :rtype: dict
#         :return: A dict with the following structure:
#             {
#                 'product_tmpl_id': int,
#                 'id': int,
#                 'description_sale': str|False,
#                 'display_name': str,
#                 'price': float,
#                 'quantity': int
#                 'attribute_line': [{
#                     'id': int
#                     'attribute': {
#                         'id': int
#                         'name': str
#                         'display_type': str
#                     },
#                     'attribute_value': [{
#                         'id': int,
#                         'name': str,
#                         'price_extra': float,
#                         'html_color': str|False,
#                         'image': str|False,
#                         'is_custom': bool
#                     }],
#                     'selected_attribute_id': int,
#                 }],
#                 'exclusions': dict,
#                 'archived_combination': dict,
#                 'parent_exclusions': dict,
#             }
#         """
#         product_uom = request.env['uom.uom'].browse(product_uom_id)
#         product = product_template._get_variant_for_combination(combination)
#         attribute_exclusions = product_template._get_attribute_exclusions(
#             parent_combination=parent_combination,
#             combination_ids=combination.ids,
#         )
#         product_or_template = product or product_template
#
#         values = dict(
#             product_tmpl_id=product_template.id,
#             **self._get_basic_product_information(
#                 product_or_template,
#                 pricelist,
#                 combination,
#                 quantity=quantity,
#                 uom=product_uom,
#                 currency=currency,
#                 date=so_date,
#                 **kwargs,
#             ),
#             quantity=quantity,
#             attribute_lines=[dict(
#                 id=ptal.id,
#                 attribute=dict(**ptal.attribute_id.read(['id', 'name', 'display_type'])[0]),
#                 attribute_values=[
#                     dict(
#                         **ptav.read(['name', 'html_color', 'image', 'is_custom'])[0],
#                         price_extra=self._get_ptav_price_extra(
#                             ptav, currency, so_date, product_or_template
#                         ),
#                     ) for ptav in ptal.product_template_value_ids
#                     if ptav.ptav_active or combination and ptav.id in combination.ids
#                 ],
#                 selected_attribute_value_ids=combination.filtered(
#                     lambda c: ptal in c.attribute_line_id
#                 ).ids,
#                 create_variant=ptal.attribute_id.create_variant,
#             ) for ptal in product_template.attribute_line_ids],
#             exclusions=attribute_exclusions['exclusions'],
#             archived_combinations=attribute_exclusions['archived_combinations'],
#             parent_exclusions=attribute_exclusions['parent_exclusions'],
#         )
#         # Shouldn't be sent client-side
#         values.pop('pricelist_rule_id', None)
#         return values

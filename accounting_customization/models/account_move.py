# -*- coding: utf-8 -*-
from mpmath.calculus.extrapolation import limit
from odoo import models, fields, api,_
from odoo.osv import expression

import json
import logging
from odoo.tools import float_is_zero, float_compare
from odoo.tools.misc import formatLang

_logger = logging.getLogger(__name__)

from collections import defaultdict, deque

class AccountMove(models.Model):
    _inherit = 'account.move'

    barcode = fields.Char(
        string='Barcode', 
        required=False)

    order_number = fields.Char(
        string='Order Number',
        required=False)

    pricelist_id = fields.Many2one('product.pricelist', string='Pricelist')

    reference_number = fields.Char(
        string='Reference Number',
        required=False)

    quotation_number = fields.Char(
        string='Quotation Number',
        compute='_compute_quotation_number',
        readonly=True,
    )

    opportunity_id = fields.Many2one(
        comodel_name='crm.lead',
        string='Opportunity',
        required=False)

    discount_id = fields.Many2one(
        comodel_name='discount.reason',
        string='Discount Reason',
        required=False)

    channel_id = fields.Many2one(
        comodel_name='channel.channel',
        string='Channel',
        required=False)

    sales_rep_id = fields.Many2one(
        comodel_name='sales.rep',
        string='Sales Rep',
        required=False)

    payment_journal_names = fields.Char(
        string='Payment Journal(s)',
        compute='_compute_payment_snapshot',
        store=True,
        readonly=True,
    )
    payment_amount_total = fields.Monetary(
        string='Payment Amount',
        compute='_compute_payment_snapshot',
        store=True,
        readonly=True,
        currency_field='currency_id',
    )

    inv_type = fields.Selection(
        string='Invoice Type', default='invoice',
        selection=[('sro', 'SRO'), ('quotation', 'Quotation'),('payment','Payment'),
                   ('invoice', 'Invoice'), ('debit', 'Debit')],
        required=True, )

    create_credit = fields.Boolean(
        string='Create credit',
        required=False)

    bank_id = fields.Many2one(
        comodel_name='bank.details',
        string='Bank',
        required=False)

    courier_id = fields.Many2one(
        comodel_name='courier.courier',
        string='Courier',
        required=False)

    product_notes = fields.Char(
        string='Product Notes',
        required=False)

    company_type = fields.Selection(string='Customer Type',related="partner_id.company_type",store=True,
                                    )

    @api.depends(
        'invoice_origin',
        'reference_number',
        'invoice_line_ids.sale_line_ids.order_id.name',
        'invoice_line_ids.sale_line_ids.order_id.reference_number',
        'pos_order_ids.downpayment_source_reference_number',
        'pos_order_ids.downpayment_source_quotation_name',
        'pos_order_ids.downpayment_source_quotation_id.reference_number',
        'pos_order_ids.downpayment_source_quotation_id.name',
    )
    def _compute_quotation_number(self):
        origin_values = {
            (move.invoice_origin or '').strip()
            for move in self
            if move.invoice_origin
        }
        pos_origin_names = set()
        if origin_values and 'pos.order' in self.env:
            pos_origin_names = set(
                self.env['pos.order'].sudo().search(
                    [('name', 'in', list(origin_values))]
                ).mapped('name')
            )

        for move in self:
            sale_orders = move.invoice_line_ids.mapped('sale_line_ids.order_id')
            quotation_names = []
            for sale_order in sale_orders:
                if "reference_number" in sale_order._fields and sale_order.reference_number:
                    quotation_names.append(sale_order.reference_number)
                elif sale_order.name:
                    quotation_names.append(sale_order.name)

            normalized_names = [name for name in quotation_names if name]
            if not normalized_names and move.reference_number:
                normalized_names = [move.reference_number]
            if not normalized_names and 'pos_order_ids' in move._fields and move.pos_order_ids:
                pos_based_names = []
                for pos_order in move.pos_order_ids:
                    if (
                        "downpayment_source_quotation_id" in pos_order._fields
                        and pos_order.downpayment_source_quotation_id
                    ):
                        source = pos_order.downpayment_source_quotation_id
                        if "reference_number" in source._fields and source.reference_number:
                            pos_based_names.append(source.reference_number)
                        elif source.name:
                            pos_based_names.append(source.name)
                    elif (
                        "downpayment_source_reference_number" in pos_order._fields
                        and pos_order.downpayment_source_reference_number
                    ):
                        pos_based_names.append(pos_order.downpayment_source_reference_number)
                    elif (
                        "downpayment_source_quotation_name" in pos_order._fields
                        and pos_order.downpayment_source_quotation_name
                    ):
                        pos_based_names.append(pos_order.downpayment_source_quotation_name)
                normalized_names = [name for name in pos_based_names if name]
            if not normalized_names and move.invoice_origin:
                clean_origin = (move.invoice_origin or '').strip()
                if clean_origin and clean_origin not in pos_origin_names:
                    normalized_names = [clean_origin]

            move.quotation_number = ', '.join(dict.fromkeys(normalized_names)) if normalized_names else False

    @api.depends(
        'invoice_payments_widget',
        'payment_state',
        'amount_total',
        'amount_residual',
    )
    def _compute_payment_snapshot(self):
        for move in self:
            move.payment_journal_names = ''
            move.payment_amount_total = 0.0

            if move.move_type not in ('out_invoice', 'out_refund'):
                continue

            names = []
            amount = 0.0
            try:
                payments = move._get_reconciled_payments()
            except Exception:
                payments = self.env['account.payment']

            if payments:
                names = list(dict.fromkeys(payments.mapped('journal_id.name')))
                amount = sum(abs(float(payment.amount or 0.0)) for payment in payments)
            else:
                if 'pos_order_ids' in move._fields:
                    pos_payments = move.pos_order_ids.payment_ids
                    if pos_payments:
                        names = list(dict.fromkeys(pos_payments.mapped('payment_method_id.name')))
                        amount = sum(abs(float(payment.amount or 0.0)) for payment in pos_payments)

            if not amount:
                amount = max(abs(float(move.amount_total or 0.0)) - abs(float(move.amount_residual or 0.0)), 0.0)

            move.payment_journal_names = ', '.join(names)
            move.payment_amount_total = amount

    def get_product_notes(self):
        for rec in self.invoice_line_ids:
            if rec.product_id.product_notes:
                return rec.product_id.product_notes
            else:
                return ''
    en_comment = fields.Html(
        string="En_comment",default= lambda self: self.env.company.sale_note_en,
        required=False)
    comment = fields.Html(
        string="comment",default= lambda self: self.env.company.sale_note,
        required=False)

    def get_gift_invoice(self):
        for rec in self:
            pos_order = self.env['pos.order'].search([('name','=',rec.invoice_origin),('state','=','invoiced')],limit=1)
            if pos_order:
                return pos_order.as_gift
            else:
                return False

    def _get_printable_invoice_lines(self):
        self.ensure_one()

        def _is_positive_qty(line):
            precision = line.product_uom_id.rounding if line.product_uom_id and line.product_uom_id.rounding else 0.00001
            return float_compare(line.quantity, 0.0, precision_rounding=precision) > 0

        return self.invoice_line_ids.filtered(
            lambda line: line.display_type == 'product' and line.select_for_report and _is_positive_qty(line)
        )

    def _get_report_paid_amount(self):
        self.ensure_one()
        paid_amount = 0.0
        payments_widget = self.sudo().invoice_payments_widget or {}

        if isinstance(payments_widget, str):
            try:
                payments_widget = json.loads(payments_widget)
            except Exception:
                payments_widget = {}

        if isinstance(payments_widget, dict):
            for payment_vals in payments_widget.get('content') or []:
                if payment_vals.get('is_exchange'):
                    continue
                paid_amount += abs(float(payment_vals.get('amount') or 0.0))

        if not paid_amount:
            try:
                paid_amount = sum(abs(float(payment.amount)) for payment in self._get_reconciled_payments())
            except Exception:
                paid_amount = 0.0

        return paid_amount

    def get_print_lines_summary(self):
        self.ensure_one()
        currency = self.currency_id or self.company_currency_id
        company_currency = self.company_currency_id or currency
        company = self.company_id
        convert_date = self.invoice_date or fields.Date.context_today(self)

        printable_lines = self._get_printable_invoice_lines().sorted(
            key=lambda line: (-line.sequence, line.date, line.move_name, -line.id),
            reverse=True,
        )

        printed_untaxed = sum(printable_lines.mapped('price_subtotal'))
        printed_total = sum(printable_lines.mapped('price_total'))
        printed_tax = printed_total - printed_untaxed

        if currency:
            printed_untaxed = currency.round(printed_untaxed)
            printed_tax = currency.round(printed_tax)
            printed_total = currency.round(printed_total)

        printed_paid = min(self._get_report_paid_amount(), printed_total)
        printed_due = max(printed_total - printed_paid, 0.0)
        if currency:
            printed_paid = currency.round(printed_paid)
            printed_due = currency.round(printed_due)

        if currency and company_currency and currency != company_currency:
            printed_untaxed_company = company_currency.round(
                currency._convert(printed_untaxed, company_currency, company, convert_date)
            )
            printed_tax_company = company_currency.round(
                currency._convert(printed_tax, company_currency, company, convert_date)
            )
            printed_total_company = company_currency.round(
                currency._convert(printed_total, company_currency, company, convert_date)
            )
            show_company_currency = True
        else:
            printed_untaxed_company = printed_untaxed
            printed_tax_company = printed_tax
            printed_total_company = printed_total
            show_company_currency = False

        printed_amount_words_ar = ''
        printed_amount_words_en = ''
        if currency and hasattr(currency, 'amount_to_text'):
            printed_amount_words_ar = currency.amount_to_text(printed_total)
            printed_amount_words_en = currency.amount_to_text(printed_total)
        if currency and hasattr(currency, 'en_amount_to_text'):
            printed_amount_words_en = currency.en_amount_to_text(printed_total)

        return {
            'printed_lines': printable_lines,
            'printed_untaxed': printed_untaxed,
            'printed_tax': printed_tax,
            'printed_total': printed_total,
            'printed_paid': printed_paid,
            'printed_due': printed_due,
            'printed_amount_words_ar': printed_amount_words_ar,
            'printed_amount_words_en': printed_amount_words_en,
            'show_company_currency': show_company_currency,
            'printed_untaxed_company': printed_untaxed_company,
            'printed_tax_company': printed_tax_company,
            'printed_total_company': printed_total_company,
        }

    def _get_invoiced_lot_values(self):
        """ Get and prepare data to show a table of invoiced lot on the invoice's report. """
        self.ensure_one()

        res =[]

        if self.state == 'draft' or not self.invoice_date or self.move_type not in ('out_invoice', 'out_refund'):
            return res

        current_invoice_amls = self.invoice_line_ids.filtered(lambda aml: aml.display_type == 'product' and aml.product_id and aml.product_id.type == 'consu' and aml.quantity and aml.select_for_report )
        all_invoices_amls = current_invoice_amls.sale_line_ids.invoice_lines.filtered(lambda aml: aml.move_id.state == 'posted').sorted(lambda aml: (aml.date, aml.move_name, aml.id))
        index = all_invoices_amls.ids.index(current_invoice_amls[:1].id) if current_invoice_amls[:1] in all_invoices_amls else 0
        previous_amls = all_invoices_amls[:index]
        invoiced_qties = current_invoice_amls._get_invoiced_qty_per_product()
        invoiced_products = invoiced_qties.keys()

        if self.move_type == 'out_invoice':
            # filter out the invoices that have been fully refund and re-invoice otherwise, the quantities would be
            # consumed by the reversed invoice and won't be print on the new draft invoice
            previous_amls = previous_amls.filtered(lambda aml: aml.move_id.payment_state != 'reversed')

        previous_qties_invoiced = previous_amls._get_invoiced_qty_per_product()

        if self.move_type == 'out_refund':
            # we swap the sign because it's a refund, and it would print negative number otherwise
            for p in previous_qties_invoiced:
                previous_qties_invoiced[p] = -previous_qties_invoiced[p]
            for p in invoiced_qties:
                invoiced_qties[p] = -invoiced_qties[p]

        qties_per_lot = defaultdict(float)
        previous_qties_delivered = defaultdict(float)
        stock_move_lines = current_invoice_amls.sale_line_ids.move_ids.move_line_ids.filtered(lambda sml: sml.state == 'done' and sml.lot_id).sorted(lambda sml: (sml.date, sml.id))
        for sml in stock_move_lines:
            if sml.product_id not in invoiced_products or not sml._should_show_lot_in_invoice():
                continue
            product = sml.product_id
            product_uom = product.uom_id
            quantity = sml.product_uom_id._compute_quantity(sml.quantity, product_uom)

            # is it a stock return considering the document type (should it be it thought of as positively or negatively?)
            is_stock_return = (
                    # self.move_type == 'out_invoice' and (sml.location_id.usage, sml.location_dest_id.usage) == ('customer', 'internal')
                    # or
                    self.move_type == 'out_refund' and (sml.location_id.usage, sml.location_dest_id.usage) == ('internal', 'customer')
            )
            if is_stock_return:
                returned_qty = min(qties_per_lot[sml.lot_id], quantity)
                qties_per_lot[sml.lot_id] -= returned_qty
                quantity = returned_qty - quantity

            previous_qty_invoiced = previous_qties_invoiced[product]
            previous_qty_delivered = previous_qties_delivered[product]
            # If we return more than currently delivered (i.e., quantity < 0), we remove the surplus
            # from the previously delivered (and quantity becomes zero). If it's a delivery, we first
            # try to reach the previous_qty_invoiced
            if float_compare(quantity, 0, precision_rounding=product_uom.rounding) < 0 or \
                    float_compare(previous_qty_delivered, previous_qty_invoiced, precision_rounding=product_uom.rounding) < 0:
                previously_done = quantity if is_stock_return else min(previous_qty_invoiced - previous_qty_delivered, quantity)
                previous_qties_delivered[product] += previously_done
                quantity -= previously_done

            qties_per_lot[sml.lot_id] += quantity

        added_lots = set()

        for lot, qty in qties_per_lot.items():
            lot = lot.sudo()

            # 🔥 prevent duplication in refund
            if self.move_type == 'out_refund' and lot.id in added_lots:
                continue
            added_lots.add(lot.id)

            if float_is_zero(invoiced_qties[lot.product_id], precision_rounding=lot.product_uom_id.rounding) \
                    or float_compare(qty, 0, precision_rounding=lot.product_uom_id.rounding) <= 0:
                continue

            invoiced_lot_qty = min(qty, invoiced_qties[lot.product_id])
            invoiced_qties[lot.product_id] -= invoiced_lot_qty

            res.append({
                'product_name': lot.product_id.display_name,
                'quantity': formatLang(self.env, invoiced_lot_qty, dp='Product Unit of Measure'),
                'uom_name': lot.product_uom_id.name,
                'lot_name': lot.name,
                'lot_id': lot.id,
            })

        for order in self.sudo().pos_order_ids:
            for line in order.lines:
                lots = line.pack_lot_ids or False
                if lots:
                    for lot in lots:
                        res.append({
                            'product_name': lot.product_id.display_name,
                            'quantity': line.qty if lot.product_id.tracking == 'lot' else 1.0,
                            'uom_name': line.product_uom_id.name,
                            'lot_name': lot.lot_name,
                            'pos_lot_id': lot.id,
                        })


        return res

    def get_grouped_lot_values(self):
        self.ensure_one()
        raw = self._get_invoiced_lot_values()
        grouped = {}

        for line in raw:
            product = line.get('product_name')
            lot = line.get('lot_name')
            qty = float(line.get('quantity') or 0)  # ← تحويل الكمية إلى float
            uom = line.get('uom_name')

            if product not in grouped:
                grouped[product] = {
                    "product_name": product,
                    "quantity": 0.0,
                    "serials": set(),
                    "uom_name": uom,
                }

            grouped[product]["quantity"] += qty
            grouped[product]["serials"].add(lot)

        result = []
        for p, vals in grouped.items():
            vals["serials"] = " , ".join(sorted(vals["serials"]))
            result.append(vals)

        return result

    tax_t1 = fields.Float(compute='_compute_tax', string="VAT14%")
    tax_t2 = fields.Float(compute='_compute_tax', string="VAT1%")
    tax_t3 = fields.Float(compute='_compute_tax', string="VAT3%")
    tax_t5 = fields.Float(compute='_compute_tax', string="VAT5%")
    tax_t2_t = fields.Float(compute='_compute_tax', string="VAT2%")
    total = fields.Float(compute='compute_tax', string="Total")

    @api.depends('invoice_line_ids')
    def _compute_tax(self):
        for rec in self:
            sum_v14 = 0
            sum_v1 = 0
            sum_v3 = 0
            tax_t2_t = 0
            sum_v5 = 0

            for line in rec.invoice_line_ids:
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
                            tax_t2_t += (line.price_subtotal * tax.amount / 100)


            rec.tax_t1 = sum_v14
            rec.tax_t2 = sum_v1
            rec.tax_t3 = sum_v3
            rec.tax_t5 = sum_v5
            rec.tax_t2_t = tax_t2_t
            rec.total = sum_v14 + rec.amount_untaxed




    def action_post(self):

        res = super(AccountMove, self).action_post()
        for rec in self:
            if rec.move_type in ('out_invoice', 'out_refund'):
                # if rec.partner_id.company_type == 'company':
                try:
                    rec.action_send_electronic_invoice()
                except:
                    pass

        return res

class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    select_for_report = fields.Boolean(
        string='Select For Report',default=True,
        required=False)

    item_code = fields.Char(
        string='Item Code',
        required=False)


    warranty_id = fields.Many2one(
        comodel_name='product.warranty',
        string='Warranty',
        required=False)


    family_id = fields.Many2one(
        comodel_name='product.family',
        string='Family',
        required=False)

    categ_id = fields.Many2one(
        comodel_name='product.category',
        string='Category',
        required=False)
    sub_categ_id = fields.Many2one(
        comodel_name='sub.category',
        string='Sub Category',
        related='product_id.product_tmpl_id.sub_categ_id',
        store=True,
        readonly=True,
    )

    product_point = fields.Float(
        string='Product point',
        required=False)
    product_incentive = fields.Float(
        string='Product incentive',
        required=False)



    class AccountMoveReversal(models.TransientModel):
        _inherit = 'account.move.reversal'

        def _prepare_default_reversal(self, move):
            res = super()._prepare_default_reversal(move)
            res.update({
                'barcode': move.barcode,
                'reference_number': move.reference_number,
                'opportunity_id': move.opportunity_id.id,
                'discount_id': move.discount_id.id,
                'channel_id': move.channel_id.id,
                'sales_rep_id': move.sales_rep_id.id,
                'inv_type': move.inv_type,
                'bank_id': move.bank_id.id,
                'courier_id': move.courier_id.id
            })
            return res

        def reverse_moves(self, is_modify=False):
            self.ensure_one()
            moves = self.move_ids

            # Create default values.
            partners = moves.company_id.partner_id + moves.commercial_partner_id

            bank_ids = self.env['res.partner.bank'].search([
                ('partner_id', 'in', partners.ids),
                ('company_id', 'in', moves.company_id.ids + [False]),
            ], order='sequence DESC')
            partner_to_bank = {bank.partner_id: bank for bank in bank_ids}
            default_values_list = []
            for move in moves:
                if move.is_outbound():
                    partner = move.company_id.partner_id
                else:
                    partner = move.commercial_partner_id
                default_values_list.append({
                    'partner_bank_id': partner_to_bank.get(partner, self.env['res.partner.bank']).id,
                    **self._prepare_default_reversal(move),
                })

            batches = [
                [self.env['account.move'], [], True],  # Moves to be cancelled by the reverses.
                [self.env['account.move'], [], False],  # Others.
            ]
            for move, default_vals in zip(moves, default_values_list):
                is_auto_post = default_vals.get('auto_post') != 'no'
                is_cancel_needed = not is_auto_post and (is_modify or self.move_type == 'entry')
                batch_index = 0 if is_cancel_needed else 1
                batches[batch_index][0] |= move
                batches[batch_index][1].append(default_vals)

            # Handle reverse method.
            moves_to_redirect = self.env['account.move']
            for moves, default_values_list, is_cancel_needed in batches:
                new_moves = moves._reverse_moves(default_values_list, cancel=is_cancel_needed)
                moves._message_log_batch(
                    bodies={move.id: move.env._('This entry has been %s',
                                                reverse._get_html_link(title=move.env._("reversed"))) for move, reverse
                            in zip(moves, new_moves)}
                )

                if is_modify:
                    moves_vals_list = []
                    for move in moves.with_context(include_business_fields=True):
                        data = move.copy_data(self._modify_default_reverse_values(move))[0]
                        data['line_ids'] = [line for line in data['line_ids'] if
                                            line[2]['display_type'] in ('product', 'line_section', 'line_note')]
                        moves_vals_list.append(data)
                    new_moves = self.env['account.move'].create(moves_vals_list)

                moves_to_redirect |= new_moves

            self.new_move_ids = moves_to_redirect
            # for move in self.new_move_ids:
            #     move.action_post()
            # Create action.
            action = {
                'name': _('Reverse Moves'),
                'type': 'ir.actions.act_window',
                'res_model': 'account.move',
            }
            if len(moves_to_redirect) == 1:
                action.update({
                    'view_mode': 'form',
                    'res_id': moves_to_redirect.id,
                    'context': {'default_move_type': moves_to_redirect.move_type},
                })
            else:
                action.update({
                    'view_mode': 'list,form',
                    'domain': [('id', 'in', moves_to_redirect.ids)],
                })
                if len(set(moves_to_redirect.mapped('move_type'))) == 1:
                    action['context'] = {'default_move_type': moves_to_redirect.mapped('move_type').pop()}
            return action

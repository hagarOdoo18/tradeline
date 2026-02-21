from odoo import models, fields, api


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    # =============================================
    # Related Fields
    # =============================================

    opportunity_id = fields.Many2one(
        related='move_id.opportunity_id',
        string='Opportunity',
        store=True,
        readonly=True,
    )
    sales_rep_id = fields.Many2one(
        related='move_id.sales_rep_id',
        string='Sales Rep',
        store=True,
        readonly=True,
    )
    channel_id = fields.Many2one(
        related='move_id.channel_id',
        string='Channel',
        store=True,
        readonly=True,
    )
    reference_number = fields.Char(
        related='move_id.reference_number',
        string='PO',
        store=True,
        readonly=True,
    )
    partner_mobile = fields.Char(
        related='partner_id.mobile',
        string='Customer Mobile',
        readonly=True,
    )
    partner_phone = fields.Char(
        related='partner_id.phone',
        string='Customer Phone',
        readonly=True,
    )


    product_upc = fields.Char(
        string='UPC',
        readonly=True,
    )

    standard_price = fields.Float(
        string='Unit Cost',
        readonly=True,
    )
    vendor_id = fields.Many2one(
        string='Vendor',
        store=True,
        readonly=True,
    )
    amount_untaxed_signed = fields.Monetary(
        related='move_id.amount_untaxed_signed',
        string='Invoice Amount Signed',
        store=True,
        readonly=True,
    )
    amount_total_signed_move = fields.Monetary(
        related='move_id.amount_total_signed',
        string='Invoice Price Total Signed',
        store=True,
        readonly=True,
    )
    move_type = fields.Selection(
        related='move_id.move_type',
        store=True,
        readonly=True,
    )

    # =============================================
    # Computed Fields
    # =============================================
    credit_note = fields.Char(
        string='Credit Note',
        compute='_compute_credit_note',
        store=False,
    )
    total_cost = fields.Float(
        string='Total Cost',
        compute='_compute_total_cost',
        store=True,
        digits='Product Price',
    )
    signed_quantity = fields.Float(
        string='Signed Quantity',
        compute='_compute_signed_values',
        store=True,
    )
    amount_signed = fields.Monetary(
        string='Amount Signed',
        compute='_compute_signed_values',
        store=True,
        currency_field='currency_id',
    )
    price_total_signed = fields.Monetary(
        string='Price Total Signed',
        compute='_compute_signed_values',
        store=True,
        currency_field='currency_id',
    )
    payment_journals = fields.Char(
        string='Payment Journals',
        compute='_compute_payment_info',
        store=False,
    )
    payment_amount_journals = fields.Char(
        string='Payment Amount Journals',
        compute='_compute_payment_info',
        store=False,
    )
    serial_display = fields.Char(
        string='Serial',
        compute='_compute_serial_display',
        store=False,
    )

    # =============================================
    # Compute Methods
    # =============================================

    @api.depends('move_id', 'move_id.reversed_entry_id')
    def _compute_credit_note(self):
        """Get linked credit note references for the invoice."""
        for line in self:
            credit = ''
            if line.move_id and line.move_id.reversed_entry_id:
                credit = ', '.join(line.move_id.reversed_entry_id.mapped('name'))
            line.credit_note = credit

    @api.depends('product_id.standard_price', 'quantity', 'move_id.move_type')
    def _compute_total_cost(self):
        """Compute total cost = standard_price * quantity, negated for refunds."""
        for line in self:
            cost = line.product_id.standard_price * line.quantity
            if line.move_id.move_type == 'out_refund':
                cost *= -1
            line.total_cost = cost

    @api.depends('quantity', 'price_subtotal', 'price_total', 'move_id.move_type')
    def _compute_signed_values(self):
        """Negate quantity, subtotal, and total for credit notes (out_refund)."""
        for line in self:
            sign = -1 if line.move_id.move_type == 'out_refund' else 1
            line.signed_quantity = line.quantity * sign
            line.amount_signed = line.price_subtotal * sign
            line.price_total_signed = line.price_total * sign

    @api.depends('move_id')
    def _compute_payment_info(self):
        """Get payment journal names and amounts from reconciled payments or POS."""
        for line in self:
            payments = line.move_id._get_reconciled_payments()
            if payments:
                line.payment_journals = ', '.join(payments.mapped('journal_id.name'))
                line.payment_amount_journals = ', '.join(
                    str(p.amount) for p in payments
                )
            else:
                # Fallback to POS payments
                pos_payments = line.move_id.pos_order_ids.payment_ids
                if pos_payments:
                    line.payment_journals = ', '.join(
                        pos_payments.mapped('payment_method_id.name')
                    )
                    line.payment_amount_journals = ', '.join(
                        str(p.amount) for p in pos_payments
                    )
                else:
                    line.payment_journals = ''
                    line.payment_amount_journals = ''

    @api.depends('move_id', 'product_id')
    def _compute_serial_display(self):
        """Get serial/lot numbers linked to this invoice line's product."""
        for line in self:
            try:
                lot_values = line.move_id._get_invoiced_lot_values()
                serials = [
                    str(d['lot_name'])
                    for d in lot_values
                    if d.get('product_name') == line.product_id.display_name
                ]
                line.serial_display = ', '.join(serials) if serials else ''
            except Exception:
                line.serial_display = ''

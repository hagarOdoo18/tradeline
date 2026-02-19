from odoo import models, fields, api, _
from odoo.exceptions import UserError


class SaleOrder(models.Model):
    _inherit = 'sale.order'


    returned_payment_count = fields.Integer(
        string='Returned Payments',
        compute='_compute_returned_payment_count',
    )
    has_returnable_payments = fields.Boolean(
        string='Has Returnable Payments',
        compute='_compute_has_returnable_payments',
    )

    @api.depends(
        'payment_ids',
    )
    def _compute_has_returnable_payments(self):
        for order in self:
            payments = order._get_so_linked_payments()
            order.has_returnable_payments = bool(payments)

    @api.depends('payment_ids')
    def _compute_returned_payment_count(self):
        for order in self:
            order.returned_payment_count = len(order.payment_ids.filtered(
                lambda p: p and p.state == 'paid' and p.payment_type == 'outbound'
            ).ids)

    def _get_so_linked_payments(self):
        """Get all posted inbound payments linked to this sale order
        that have not yet been returned."""
        self.ensure_one()
        payments = self.env['account.payment']

        # 1) Payments from payment transactions linked to SO
        if self.payment_ids:
            payments |=self.payment_ids.filtered(
                lambda p: p and p.state == 'paid' and p.payment_type == 'inbound'
            )



        # 3) Payments linked through invoice reconciliation


        # Exclude payments that already have been returned
        already_returned_ids =  self.payment_ids.filtered(
                lambda p: p and p.state == 'paid' and p.payment_type == 'outbound'
            )
        payments = payments.filtered(lambda p: p.id not in already_returned_ids.mapped('reversed_original_payment_id').ids)

        return payments

    def action_return_payments(self):
        """Create outbound (refund) account.payment for each linked inbound payment."""
        self.ensure_one()

        payments = self._get_so_linked_payments()

        if not payments:
            raise UserError(_(
                "No returnable payments found for this sale order.\n"
                "Either there are no posted payments linked, "
                "or all payments have already been returned."
            ))

        created_returns = self.env['account.payment']

        for payment in payments:
            return_vals = {
                'payment_type': 'outbound',
                'partner_type': 'customer',
                'partner_id': payment.partner_id.id,
                'amount': payment.amount,
                'currency_id': payment.currency_id.id,
                'journal_id': payment.journal_id.id,
                'branch_id': payment.branch_id.id,
                'sale_order_id': self.id,
                'reversed_original_payment_id': payment.id,
            }

            # Copy payment method line if available
            outbound_method_line = payment.journal_id.outbound_payment_method_line_ids[:1]
            if outbound_method_line:
                return_vals['payment_method_line_id'] = outbound_method_line.id

            return_payment = self.env['account.payment'].create(return_vals)
            return_payment.action_post()
            created_returns |= return_payment

        # Navigate to created return payments
        if len(created_returns) == 1:
            return {
                'name': _('Return Payment'),
                'type': 'ir.actions.act_window',
                'res_model': 'account.payment',
                'view_mode': 'form',
                'res_id': created_returns.id,
                'target': 'current',
            }
        return {
            'name': _('Return Payments'),
            'type': 'ir.actions.act_window',
            'res_model': 'account.payment',
            'view_mode': 'list,form',
            'domain': [('id', 'in', created_returns.ids)],
            'target': 'current',
        }

    def action_view_returned_payments(self):
        """View all return payments created from this SO."""
        self.ensure_one()
        action = {
            'name': _('Returned Payments'),
            'type': 'ir.actions.act_window',
            'res_model': 'account.payment',
            'view_mode': 'list,form',
            'domain': [('id', 'in', self.payment_ids.ids),('payment_type','=','outbound')],
            'context': {'create': False},
            'target': 'current',
        }
        # if len(self.returned_payment_ids) == 1:
        #     action['view_mode'] = 'form'
        #     action['res_id'] = self.returned_payment_ids.id
        return action

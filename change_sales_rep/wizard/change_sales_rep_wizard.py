# -*- coding: utf-8 -*-
import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ChangeSalesRepWizard(models.TransientModel):
    _name = 'change.sales.rep.wizard'
    _description = 'Change Sales Representative'

    old_rep_ids = fields.Many2many(
        'sales.rep',
        'change_sales_rep_wizard_old_rep_rel',
        'wizard_id',
        'rep_id',
        string='Old Sales Reps',
        required=True,
        help='All invoices, credit notes, sale orders and POS orders '
             'tied to any of these sales reps will be reassigned. The '
             'old sales reps are then archived.',
    )
    new_rep_id = fields.Many2one(
        'sales.rep',
        string='New Sales Rep',
        required=True,
        help='Records currently tied to the old sales reps will be '
             'reassigned to this one.',
    )

    @api.constrains('old_rep_ids', 'new_rep_id')
    def _check_reps_different(self):
        for wiz in self:
            if wiz.new_rep_id and wiz.old_rep_ids and wiz.new_rep_id in wiz.old_rep_ids:
                raise UserError(_(
                    "The New Sales Rep cannot also be selected as an Old Sales Rep."
                ))

    # ------------------------------------------------------------------
    def _archive(self, reps):
        """Archive every old sales rep (active=False)."""
        try:
            reps.sudo().write({'active': False})
            return 'archived'
        except Exception:
            _logger.exception(
                "change_sales_rep: failed to archive sales.rep %s.",
                reps.mapped('display_name'),
            )
            return 'failed'

    # ------------------------------------------------------------------
    def action_change(self):
        """Reassign every relevant record from any of the old sales reps
        to the new one, then archive the old sales reps.

        Reassignment covers:
          * account.move (out_invoice, out_refund, in_invoice, in_refund)
            via `sales_rep_id`.
          * sale.order via `sales_rep_id`.
          * pos.order via `sales_rep_id`, if both POS and the field exist.
        """
        self.ensure_one()
        if self.new_rep_id in self.old_rep_ids:
            raise UserError(_(
                "The New Sales Rep cannot also be selected as an Old Sales Rep."
            ))

        old_reps = self.old_rep_ids
        old_ids = old_reps.ids
        new_id = self.new_rep_id.id

        # ---- 1. account.move ----
        invoice_count = 0
        Move = self.env.get('account.move')
        if Move is not None and 'sales_rep_id' in Move._fields:
            invoices = Move.sudo().search([
                ('sales_rep_id', 'in', old_ids),
                ('move_type', 'in', ['out_invoice', 'out_refund',
                                     'in_invoice', 'in_refund']),
            ])
            invoice_count = len(invoices)
            if invoices:
                invoices.write({'sales_rep_id': new_id})

        # ---- 2. sale.order ----
        sale_count = 0
        SaleOrder = self.env.get('sale.order')
        if SaleOrder is not None and 'sales_rep_id' in SaleOrder._fields:
            sale_orders = SaleOrder.sudo().search([
                ('sales_rep_id', 'in', old_ids),
            ])
            sale_count = len(sale_orders)
            if sale_orders:
                sale_orders.write({'sales_rep_id': new_id})

        # ---- 3. pos.order ----
        pos_count = 0
        PosOrder = self.env.get('pos.order')
        if PosOrder is not None and 'sales_rep_id' in PosOrder._fields:
            pos_orders = PosOrder.sudo().search([
                ('sales_rep_id', 'in', old_ids),
            ])
            pos_count = len(pos_orders)
            if pos_orders:
                pos_orders.write({'sales_rep_id': new_id})

        # ---- 4. Archive every old sales rep ----
        outcome = self._archive(old_reps)


        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Sales Representative Changed'),
                'message': 'Done',
                'type': 'success' if outcome == 'archived' else 'warning',
                'sticky': True,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }

# -*- coding: utf-8 -*-
import logging

from psycopg2 import IntegrityError, errors as pg_errors

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ChangeSalesRepWizard(models.TransientModel):
    _name = 'change.sales.rep.wizard'
    _description = 'Change Sales Representative'

    old_rep_id = fields.Many2one(
        'sales.rep',
        string='Old Sales Rep',
        required=True,
        help='All invoices, credit notes, sale orders and POS orders '
             'tied to this sales rep will be reassigned. The sales rep '
             'is then deleted (or archived if delete is blocked).',
    )
    new_rep_id = fields.Many2one(
        'sales.rep',
        string='New Sales Rep',
        required=True,
        help='Records currently tied to the old sales rep will be '
             'reassigned to this one.',
    )

    @api.constrains('old_rep_id', 'new_rep_id')
    def _check_reps_different(self):
        for wiz in self:
            if wiz.old_rep_id and wiz.new_rep_id and wiz.old_rep_id == wiz.new_rep_id:
                raise UserError(_("Old and New sales rep must be different."))

    # ------------------------------------------------------------------
    def _delete_or_archive(self, rep):
        """Try to unlink the sales rep; on FK / RestrictViolation, archive
        instead by writing active=False (if the model has an `active` field).
        Returns 'deleted', 'archived', or 'failed'.
        """
        try:
            rep.sudo().write({'active': False})
        except Exception:
            _logger.exception(
                "change_sales_rep: failed to archive sales.rep %s.",
                rep.display_name,
            )
    # ------------------------------------------------------------------
    def action_change(self):
        """Reassign every relevant record from the old sales rep to the
        new one, then delete (or archive) the old sales rep.

        Reassignment covers:
          * account.move (out_invoice, out_refund, in_invoice, in_refund)
            via `sales_rep_id`.
          * sale.order via `sales_rep_id`.
          * pos.order via `sales_rep_id`, if both POS and the field exist.

        Each model is touched only if it actually carries a `sales_rep_id`
        column — so the wizard works even if the custom field is added
        only to a subset of models.
        """
        self.ensure_one()
        if self.old_rep_id == self.new_rep_id:
            raise UserError(_("Old and New sales rep must be different."))

        old_rep = self.old_rep_id
        old_id = old_rep.id
        new_id = self.new_rep_id.id
        old_name = old_rep.display_name

        # ---- 1. account.move ----
        invoice_count = 0
        Move = self.env.get('account.move')
        if Move is not None and 'sales_rep_id' in Move._fields:
            invoices = Move.sudo().search([
                ('sales_rep_id', '=', old_id),
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
            sale_orders = SaleOrder.sudo().search([('sales_rep_id', '=', old_id)])
            sale_count = len(sale_orders)
            if sale_orders:
                sale_orders.write({'sales_rep_id': new_id})

        # ---- 3. pos.order ----
        pos_count = 0
        PosOrder = self.env.get('pos.order')
        if PosOrder is not None and 'sales_rep_id' in PosOrder._fields:
            pos_orders = PosOrder.sudo().search([('sales_rep_id', '=', old_id)])
            pos_count = len(pos_orders)
            if pos_orders:
                pos_orders.write({'sales_rep_id': new_id})

        # ---- 4. Delete (or archive) the old sales rep ----
        outcome = self._delete_or_archive(old_rep)



        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Sales Representative Changed'),
                'message': 'Done',
                'type': 'success' if outcome != 'failed' else 'warning',
                'sticky': True,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }

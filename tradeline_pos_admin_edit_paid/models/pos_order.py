import logging

from odoo import models

_logger = logging.getLogger(__name__)

LOCKED_STATES = ('paid', 'done', 'invoiced')


class PosOrder(models.Model):
    _inherit = 'pos.order'

    def write(self, vals):
        # Core pos.order.write raises:
        #   "This order has already been paid. You cannot set it back to
        #    draft or edit it."
        # for any order not in 'draft'. For a system administrator we clear
        # that state directly in SQL (bypassing the ORM guard) so the core
        # check passes, then let the normal write proceed.
        #
        # NOTE: this demotes the order to 'draft' in the database only. It
        # does NOT unwind payments, invoices or stock moves. Reconciling the
        # resulting records is a manual, admin responsibility.
        if self.env.user.has_group('base.group_system'):
            locked = self.filtered(lambda o: o.state in LOCKED_STATES)
            if locked:
                _logger.info(
                    "Admin %s editing paid POS order(s) %s; bypassing paid-lock.",
                    self.env.user.login, locked.ids,
                )
                self.env.cr.execute(
                    "UPDATE pos_order SET state = 'draft' WHERE id IN %s",
                    (tuple(locked.ids),),
                )
                locked.invalidate_recordset(['state'])
        return super().write(vals)

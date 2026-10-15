import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ProductPricelist(models.Model):
    _inherit = 'product.pricelist'

    pricelist_item_count = fields.Integer(
        string='Price Rules',
        compute='_compute_pricelist_item_count',
        help='Number of price rules currently in this pricelist.',
    )

    @api.depends('item_ids')
    def _compute_pricelist_item_count(self):
        for pricelist in self:
            pricelist.pricelist_item_count = len(pricelist.item_ids)

    def action_clear_items(self):
        """Delete every price rule of the selected pricelist(s).

        Destructive and not reversible, so the button carries a confirmation
        and the deletion is written to the log with the user who ran it.
        """
        items = self.mapped('item_ids')
        if not items:
            raise UserError(_('This pricelist has no price rules to clear.'))

        count = len(items)
        _logger.info(
            'Pricelist clear: user %s (id=%s) deleting %s price rule(s) from '
            'pricelist(s) %s',
            self.env.user.login, self.env.uid, count, self.mapped('display_name'),
        )
        items.unlink()

        message = _('%s price rule(s) deleted.') % count
        action = {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'title': _('Pricelist cleared'),
                'message': message,
                'sticky': False,
            },
        }
        if len(self) == 1:
            # Reload the form so the emptied rules list is shown straight away
            action['params']['next'] = {
                'type': 'ir.actions.act_window',
                'res_model': 'product.pricelist',
                'res_id': self.id,
                'views': [(False, 'form')],
                'target': 'current',
            }
        return action

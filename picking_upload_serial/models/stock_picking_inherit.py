# -*- coding: utf-8 -*-
from odoo import models, _


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    def action_open_upload_excel_wizard(self):
        """Open upload wizard for the current delivery order"""
        return {
            'name': _('Upload Delivery Excel'),
            'type': 'ir.actions.act_window',
            'res_model': 'upload.delivery.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_picking_id': self.id,
            }
        }

    def _upload_validate_now(self):
        """button_validate with the avoidable overhead switched off.

        `tracking_disable` skips the chatter tracking Odoo would otherwise
        compute for every write done during validation, which is a large part
        of the cost on transfers with thousands of operation lines.

        The return value of button_validate is passed straight back: if Odoo
        asks for a backorder confirmation, the caller returns that action and
        the user still gets the popup.
        """
        self.ensure_one()
        return self.with_context(
            tracking_disable=True,
            skip_sms=True,
        ).button_validate()

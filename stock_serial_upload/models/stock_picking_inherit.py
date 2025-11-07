# -*- coding: utf-8 -*-
from odoo import models, _

class StockPicking(models.Model):
    _inherit = 'stock.picking'

    def action_open_upload_serial_excel_wizard(self):
        """Open upload wizard for the current delivery order"""
        return {
            'name': _('Upload Serial Transfer Excel'),
            'type': 'ir.actions.act_window',
            'res_model': 'upload.serial.only.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_picking_id': self.id,
            }
        }

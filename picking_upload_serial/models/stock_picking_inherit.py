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

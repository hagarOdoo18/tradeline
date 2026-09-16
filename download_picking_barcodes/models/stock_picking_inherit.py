# -*- coding: utf-8 -*-
from odoo import models

class StockPicking(models.Model):
    _inherit = 'stock.picking'

    def action_open_barcode_download_wizard(self):
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'download.picking.barcodes.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_picking_id': self.id},
        }

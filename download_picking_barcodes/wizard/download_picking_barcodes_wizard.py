# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError
import io
import base64
import xlsxwriter
from datetime import datetime

class DownloadPickingBarcodesWizard(models.TransientModel):
    _name = 'download.picking.barcodes.wizard'
    _description = 'Download Picking Barcodes Wizard'

    picking_id = fields.Many2one('stock.picking', string='Picking', required=True)
    file_data = fields.Binary('File')
    filename = fields.Char('Filename')

    def action_download_barcodes(self):
        picking = self.picking_id
        lines = picking.move_line_ids_without_package
        if not lines:
            raise UserError(_("No lines found in this picking."))

        barcodes = []
        for line in lines:
            barcode = line.product_id.barcode
            if barcode:
                barcodes.append(barcode)

        if not barcodes:
            raise UserError(_("No products with barcodes found."))

        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output, {'in_memory': True})
        sheet = workbook.add_worksheet("Barcodes")

        header_format = workbook.add_format({'bold': True, 'align': 'center', 'border': 1})
        text_format = workbook.add_format({'align': 'center', 'border': 1})

        sheet.write(0, 0, 'code', header_format)
        sheet.write(0, 1, 'serial', header_format)
        sheet.write(0, 2, 'quantity', header_format)

        row = 1
        for bc in barcodes:
            sheet.write(row, 0, bc, text_format)
            row += 1

        workbook.close()
        output.seek(0)

        self.file_data = base64.b64encode(output.read())
        self.filename = f"Picking_{picking.name}_Barcodes_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

        return {
            'type': 'ir.actions.act_url',
            'url': f"/web/content/?model={self._name}&id={self.id}&field=file_data&filename_field=filename&download=true",
            'target': 'self',
        }

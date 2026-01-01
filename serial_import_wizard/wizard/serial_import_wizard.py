import base64
import xlrd
from odoo import models, fields
from datetime import datetime

class SerialImportWizard(models.TransientModel):
    _name = 'serial.import.wizard'
    _description = 'Serial Import Wizard'

    file = fields.Binary(string="Excel File", required=True)
    filename = fields.Char()
    product_id = fields.Many2one('product.product', required=True)

    def action_import(self):
        book = xlrd.open_workbook(
            file_contents=base64.b64decode(self.file)
        )
        sheet = book.sheet_by_index(0)

        year = datetime.now().year

        for row in range(1, sheet.nrows):
            serial = str(sheet.cell(row, 0).value).strip()
            if serial:
               old= self.env['stock.lot'].search[('name','=',serial),('product_qty','=',0)]
               old.name = serial+'/'

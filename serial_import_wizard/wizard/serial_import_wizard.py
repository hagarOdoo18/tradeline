import base64
import openpyxl
from odoo import models, fields, _
from odoo.exceptions import UserError
from io import BytesIO

class SerialImportWizard(models.TransientModel):
    _name = 'serial.import.wizard'
    _description = 'Serial Import Wizard'

    file = fields.Binary(string="Excel File", required=True)
    filename = fields.Char()

    def action_import(self):
        data = base64.b64decode(self.file)
        workbook = openpyxl.load_workbook(filename=BytesIO(data), data_only=True)
        sheet = workbook.active
        for row in sheet.iter_rows(values_only=True):
            serial = row[0]
            if serial:
               old= self.env['stock.lot'].search([('name','=',serial),('product_qty','=',0)])
               old.name = serial+'/'

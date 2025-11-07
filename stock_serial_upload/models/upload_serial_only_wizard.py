# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError
import base64
import openpyxl
from io import BytesIO


class UploadSerialOnlyWizard(models.TransientModel):
    _name = 'upload.serial.only.wizard'
    _description = 'Upload Serials for Internal Transfers (Serial Column Only)'

    file = fields.Binary(string='Excel File', required=True)
    filename = fields.Char(string='File Name')
    picking_id = fields.Many2one('stock.picking', string='Transfer', required=True)

    def action_upload_serials(self):
        if not self.file:
            raise UserError(_('Please upload an Excel file.'))

        picking = self.picking_id
        if picking.picking_type_code != 'internal':
            raise UserError(_('This wizard only works for Internal Transfers.'))
        if picking.state != 'draft':
            raise UserError(_('This wizard only works for Draft Transfer.'))

        try:
            data = base64.b64decode(self.file)
            wb = openpyxl.load_workbook(filename=BytesIO(data), data_only=True)
            sheet = wb.active
        except Exception as e:
            raise UserError(_('Invalid Excel file. Must be .xlsx format.\n\nError: %s') % str(e))

        headers = [str(cell.value).strip().lower() for cell in next(sheet.iter_rows(min_row=1, max_row=1))]
        header_map = {name: idx for idx, name in enumerate(headers) if name}

        if 'serial' not in header_map:
            raise UserError(_('Excel must contain a column named "serial".'))

        processed, errors = 0, []

        for row in sheet.iter_rows(min_row=2):
            serial_name = str(row[header_map['serial']].value or '').strip()
            if not serial_name:
                continue

            lot = self.env['stock.lot'].search([('name', '=', serial_name),('location_id','=',self.picking_id.location_id.id)], limit=1)

            if not lot:

                    errors.append(f'Not Found serial {serial_name} at This Stock')
                    continue


            else:
                product = lot.product_id

            move = picking.move_ids_without_package.filtered(lambda mv: mv.product_id == product)
            if not move:
                move = self.env['stock.move'].create({
                    'picking_id': picking.id,
                    'product_id': product.id,
                    'name': product.display_name,
                    'product_uom_qty': 1.0,
                    'product_uom': product.uom_id.id,
                    'location_id': picking.location_id.id,
                    'location_dest_id': picking.location_dest_id.id,
                    'company_id': picking.company_id.id,
                })


                self.env['stock.move.line'].create({
                    'picking_id': picking.id,
                    'move_id': move.id,
                    'product_id': product.id,
                    'product_uom_id': product.uom_id.id,
                    'lot_id': lot.id,
                    'lot_name': lot.name,
                    'qty_done': 1.0,
                    'location_id': picking.location_id.id,
                    'location_dest_id': picking.location_dest_id.id,
                })
            else:
                move.product_uom_qty+=1
                lot=picking.move_line_ids_without_package.filtered(lambda mv: mv.lot_id == lot)
                if not lot:
                    self.env['stock.move.line'].create({
                        'picking_id': picking.id,
                        'move_id': move.id,
                        'product_id': product.id,
                        'product_uom_id': product.uom_id.id,
                        'lot_id': lot.id,
                        'lot_name': lot.name,
                        'qty_done': 1.0,
                        'location_id': picking.location_id.id,
                        'location_dest_id': picking.location_dest_id.id,
                    })

            processed += 1

        msg = f'Upload completed successfully. Serials processed: {processed}'
        if errors:
            msg += '\n\nErrors:\n' + '\n'.join(errors)

            raise UserError(_(msg))

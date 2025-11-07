# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError
import base64
import openpyxl
from io import BytesIO

class UploadDeliveryWizard(models.TransientModel):
    _name = 'upload.delivery.wizard'
    _description = 'Upload Excel to Delivery Lines'

    picking_id = fields.Many2one('stock.picking', string='Delivery', required=True)
    file = fields.Binary(string='Excel File', required=True)
    filename = fields.Char(string='File Name')
    create_if_not_exist = fields.Boolean(string='Create Serial if Not Exist', default=True)
    auto_confirm = fields.Boolean(string='Confirm Delivery Automatically', default=False)

    def action_upload_excel(self):
        if not self.file:
            raise UserError(_("Please upload an Excel file."))

        try:
            data = base64.b64decode(self.file)
            wb = openpyxl.load_workbook(filename=BytesIO(data), data_only=True)
            sheet = wb.active
        except Exception as e:
            raise UserError(_("Invalid Excel file. Make sure it's .xlsx format.\n\nError: %s") % str(e))

        headers = [cell.value for cell in next(sheet.iter_rows(min_row=1, max_row=1))]
        header_map = {str(h).lower(): i for i, h in enumerate(headers) if h}

        required = ['code', 'serial', 'quantity']
        for col in required:
            if col not in header_map:
                raise UserError(_("Missing column '%s' in Excel file.") % col)

        not_found_products = []
        processed = 0
        errors = []

        for row in sheet.iter_rows(min_row=2):
            code = str(row[header_map['code']].value or '').strip()
            serial_name = str(row[header_map['serial']].value or '').strip()
            qcell = row[header_map['quantity']].value
            try:
                quantity = float(qcell or 0)
            except Exception:
                quantity = 0

            if not code:
                continue

            product = self.env['product.product'].search([('barcode', '=', code)], limit=1)
            if not product:
                not_found_products.append(code)
                continue

            move_line = self.picking_id.move_line_ids_without_package.filtered(lambda ml: ml.product_id == product)
            move = self.picking_id.move_ids_without_package.filtered(lambda ml: ml.product_id == product)
            move.lot_ids=[]
            if not move_line:
                errors.append(f"Product {code} not found in this Delivery.")
                continue

            try:
                # handle serial-tracked products
                if product.tracking == 'serial':
                    if not serial_name:
                        errors.append(f"Missing serial number for product '{code}'.")
                        continue

                    lot = self.env['stock.lot'].search([
                        ('name', '=', serial_name),
                        ('product_id', '=', product.id)
                    ], limit=1)

                    if not lot:
                        if self.picking_id.picking_type_code =='incoming':
                            lot = self.env['stock.lot'].create({
                                'name': serial_name,
                                'product_id': product.id,
                                'company_id': self.picking_id.company_id.id,
                            })
                        else:
                            errors.append(f"Serial '{serial_name}' not found for product '{code}'.")
                            continue

                    existing_line = move_line.filtered(lambda ml: ml.lot_id == lot)
                    if existing_line:
                        line = existing_line[0]
                    else:

                        line = move_line[0].copy({
                            'lot_id': lot.id,
                            'lot_name': lot.name,
                            'qty_done': 1.0,
                        })
                        move_line[0].unlink()

                else:
                    # for non-serial products
                    if quantity <= 0:
                        errors.append(f"Invalid quantity for product '{code}'.")
                        continue

                    line = move_line[0]
                    line.qty_done = quantity

                processed += 1
            except Exception as e:
                errors.append(f"Error processing product '{code}': {str(e)}")

        # show missing products in one message (do not stop the process)
        result_msgs = []
        result_msgs.append(f"Upload Completed. Processed lines: {processed}")
        if not_found_products:
            result_msgs.append("Products not found in system:")
            result_msgs.extend(not_found_products)
            raise UserError("\n".join(result_msgs))
        if errors:
            result_msgs.append("Errors:")
            result_msgs.extend(errors)
            raise UserError("\n".join(result_msgs))

        # auto confirm delivery is now moved to separate function and will be called if requested
        if self.auto_confirm:
            try:
                self.action_auto_confirm()
            except UserError as e:
                result_msgs.append(f"Auto-confirm failed: {e.name if hasattr(e,'name') else str(e)}")

        # raise a user friendly message with summary
        return {
            "type": "ir.actions.client",
            "tag": "reload",
        }

    def action_auto_confirm(self):
        """Separate confirm function - can be called from the wizard or elsewhere."""
        try:
            self.picking_id.button_validate()
        except Exception as e:
            raise UserError(_("Confirmation failed: %s") % str(e))

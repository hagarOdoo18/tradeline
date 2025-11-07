# -*- coding: utf-8 -*-
from odoo import models, fields
from odoo.exceptions import UserError
import base64
import openpyxl
from io import BytesIO
import logging

_logger = logging.getLogger(__name__)


class UploadBarcodeWizard(models.TransientModel):
    _name = 'upload.product.barcode.wizard'
    _description = 'Upload Product Variant Barcodes from Excel'

    file = fields.Binary(string='Excel File', required=True)
    filename = fields.Char(string='File Name')

    def action_upload_barcodes(self):
        if not self.file:
            raise UserError('Please upload an Excel file.')

        try:
            data = base64.b64decode(self.file)
            wb = openpyxl.load_workbook(filename=BytesIO(data), data_only=True)
            sheet = wb.active
        except Exception as e:
            raise UserError(f'Invalid Excel file.\nError: {e}')

        headers = [cell.value for cell in next(sheet.iter_rows(min_row=1, max_row=1))]
        header_map = {str(h).strip().lower(): i for i, h in enumerate(headers) if h}

        required = ['display_name', 'vendor', 'barcode', 'e_code', 'gs1_code', 'color', 'capacity']
        for col in required:
            if col not in header_map:
                raise UserError(f"Missing column '{col}' in Excel file.")

        updated = 0
        not_found = []
        errors = []

        Product = self.env['product.product']

        for row in sheet.iter_rows(min_row=2):
            with self.env.cr.savepoint():  # isolate each row
                try:
                    display_name = str(row[header_map['display_name']].value or '').strip()
                    vendor = str(row[header_map['vendor']].value or '').strip()
                    barcode = str(row[header_map['barcode']].value or '').strip()
                    e_code = str(row[header_map['e_code']].value or '').strip()
                    gs1_code = str(row[header_map['gs1_code']].value or '').strip()
                    color = str(row[header_map['color']].value or '').strip()
                    capacity = str(row[header_map['capacity']].value or '').strip()

                    if not display_name:
                        continue

                    domain = [('name', '=', display_name), ('vendor_id.name', '=', vendor)]

                    # Match attributes (color/capacity)
                    if color:
                        domain.append(('product_template_variant_value_ids.product_attribute_value_id.name', '=', color))
                    if capacity:
                        domain.append(('product_template_variant_value_ids.product_attribute_value_id.name', '=', capacity))

                    product = Product.search(domain, limit=1)

                    if not product:
                        not_found.append(f"{display_name} ({color}/{capacity})")
                        continue

                    product.write({
                        'barcode': barcode or '',
                        'e_invoicing_code': e_code or '',
                        'gs1_code': gs1_code or '',
                    })
                    updated += 1

                except Exception as e:
                     # rollback current row only
                    errors.append(f"Error updating {display_name}: {str(e)}")
                    _logger.warning(f"Failed to update {display_name}: {e}")
                    continue

        # Build summary
        summary = [f"✅ Upload completed.\nUpdated variants: {updated}"]

        if not_found:
            summary.append("\n🟠 Products not found:")
            summary.extend(not_found)
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Barcode Upload Complete',
                    'message': '\n'.join(summary[:20]) + ('\n...(truncated)' if len(summary) > 20 else ''),
                    'type': 'success' if not errors else 'warning',
                    'sticky': False,
                },
            }

        if errors:
            summary.append("\n🔴 Errors:")
            summary.extend(errors)

            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Barcode Upload Complete',
                    'message': '\n'.join(summary[:20]) + ('\n...(truncated)' if len(summary) > 20 else ''),
                    'type': 'success' if not errors else 'warning',
                    'sticky': False,
                },
            }

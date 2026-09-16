# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError
import base64
from io import BytesIO
import openpyxl
from psycopg2 import IntegrityError


class ImportProductCodesWizard(models.TransientModel):
    _name = "update.product.codes.wizard"
    _description = "Import Product e_code and gs1_code by Barcode"

    file = fields.Binary(string="Excel File", required=True)
    filename = fields.Char(string="File Name")

    def action_update_codes(self):
        if not self.file:
            raise UserError(_("Please upload an Excel file."))

        # decode file
        file_data = base64.b64decode(self.file)
        workbook = openpyxl.load_workbook(BytesIO(file_data), data_only=True)
        sheet = workbook.active

        skipped_line_no = {}
        counter = 1
        updated_count = 0

        # expect columns: Barcode | e_code | gs1_code
        skip_header = True
        for row in sheet.iter_rows(values_only=True):
            try:
                if skip_header:
                    skip_header = False
                    counter += 1
                    continue

                barcode = row[0]
                e_code = row[1]
                gs1_code = row[2]

                if not barcode:
                    skipped_line_no[str(counter)] = "Missing barcode."
                    counter += 1
                    continue

                product = self.env["product.product"].search(
                    [("barcode", "=", barcode)], limit=1
                )
                if not product:
                    skipped_line_no[str(counter)] = f"Product with barcode {barcode} not found."
                    counter += 1
                    continue

                try:
                    product.write({
                        "e_invoicing_code": e_code or '',
                        "gs1_code": gs1_code or '',
                    })
                    updated_count += 1

                except IntegrityError as e:
                    self.env.cr.rollback()
                    skipped_line_no[str(counter)] = (
                        f"Duplicate e_code '{e_code}' for product {barcode}."
                    )
                except Exception as e:
                    self.env.cr.rollback()
                    skipped_line_no[str(counter)] = f"Error updating product: {str(e)}"

            except Exception as e:
                self.env.cr.rollback()
                skipped_line_no[str(counter)] = f"Row error: {str(e)}"
            finally:
                counter += 1

        # ✅ Show result message
        msg = f"{updated_count} products updated successfully."
        if skipped_line_no:
            msg += "\nSkipped lines:\n"
            for r, m in skipped_line_no.items():
                msg += f"Row {r}: {m}\n"

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Import Completed"),
                "message": msg,
                "sticky": False,
            },
        }

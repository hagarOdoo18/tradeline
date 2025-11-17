# -*- coding: utf-8 -*-
# Part of Softhealer Technologies.

from odoo import api, fields, models, _
from odoo.exceptions import UserError
from datetime import datetime
import base64
import openpyxl
from io import BytesIO


class ImportPoWizard(models.TransientModel):
    _name = "import.po.wizard"
    _description = "Import Purchase Order Wizard"

    file = fields.Binary(string="File", required=True)


    # -------------------------------------------------------------------------
    # Main Import Logic
    # -------------------------------------------------------------------------
    def import_po_apply(self):
        PurchaseOrder = self.env["purchase.order"]
        PurchaseOrderLine = self.env["purchase.order.line"]

        if not self.file:
            raise UserError(_("Please upload an Excel file."))

        data = base64.b64decode(self.file)
        workbook = openpyxl.load_workbook(filename=BytesIO(data), data_only=True)
        sheet = workbook.active

        counter = 0
        skipped_line_no = {}
        running_po = None
        created_po = False
        created_po_list_for_confirm = []
        created_po_list = []

        skip_header = True

        for row in sheet.iter_rows(values_only=True):
            try:
                if skip_header:
                    skip_header = False
                    continue

                counter += 1
                po_name = row[0]
                vendor_name = row[1]
                warehouse = row[2]
                barcode = row[3]
                qty = row[4]
                price = row[5]

                if not po_name or not vendor_name:
                    break

                # Create new PO if PO name changes
                if po_name != 'running_po':
                    partner = self.env["res.partner"].search(
                        [("name", "=", vendor_name)], limit=1
                    )
                    warehouse_id = self.env["stock.warehouse"].search(
                        [("name", "=", warehouse)], limit=1
                    )
                    if not warehouse_id:
                        skipped_line_no[str(counter)] = f"Warehouse '{warehouse}' not found."
                        break
                    else:
                        picking = self.env["stock.picking.type"].search(
                            [("warehouse_id", "=", warehouse_id.id),('code','=','incoming')], limit=1
                        )

                    if not partner:
                        skipped_line_no[str(counter)] = f"Vendor '{vendor_name}' not found."
                        continue

                    po_vals = {
                        "partner_id": partner.id,
                        "origin": po_name,
                        "branch_id": warehouse_id.branch_id.id,
                        "picking_type_id": picking.id,
                        "date_approve": datetime.now(),
                        "date_planned": datetime.now(),
                    }

                    created_po = PurchaseOrder.create(po_vals)
                    created_po_list.append(created_po.id)
                    created_po_list_for_confirm.append(created_po.id)

                if not created_po:
                    skipped_line_no[str(counter)] = "PO creation failed."
                    continue

                # Find product by barcode
                if barcode:
                    product = self.env["product.product"].search(
                        [("barcode", "=", str(barcode))], limit=1
                    )

                    if not product:
                        skipped_line_no[str(counter)] = f"Product not found for barcode {barcode}."
                        if created_po.id in created_po_list_for_confirm:
                            created_po_list_for_confirm.remove(created_po.id)
                        continue

                    qty = int(qty) if qty else 1
                    price = float(price) if price else product.standard_price

                    PurchaseOrderLine.create({
                        "order_id": created_po.id,
                        "product_id": product.id,
                        "name": product.display_name,
                        "product_qty": qty,
                        "product_uom": product.uom_po_id.id,
                        "price_unit": price,
                        "date_planned": datetime.now(),
                    })

            except Exception as e:
                skipped_line_no[str(counter)] = f"Error: {e}"
                continue

        completed_records = len(created_po_list)
        confirm_rec = len(created_po_list_for_confirm)

        # ✅ If no errors → refresh PO list
        if not skipped_line_no:
            return {
                "type": "ir.actions.client",
                "tag": "reload",
                "params": {"menu_id": self.env.ref("purchase.menu_purchase_root").id},
            }
        else:
            if skipped_line_no:
                # Display native notification for errors
                msg = "\n".join([f"Row {r}: {m}" for r, m in skipped_line_no.items()])
                return {
                    "type": "ir.actions.client",
                    "tag": "display_notification",
                    "params": {
                        "title": _("Import completed with warnings"),
                        "message": msg,
                        "sticky": True,
                    },
                }

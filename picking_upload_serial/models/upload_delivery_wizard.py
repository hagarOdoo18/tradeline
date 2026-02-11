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

    # def action_upload_excel(self):
    #     if not self.file:
    #         raise UserError(_("Please upload an Excel file."))
    #
    #     try:
    #         data = base64.b64decode(self.file)
    #         wb = openpyxl.load_workbook(filename=BytesIO(data), data_only=True)
    #         sheet = wb.active
    #     except Exception as e:
    #         raise UserError(_("Invalid Excel file. Make sure it's .xlsx format.\n\nError: %s") % str(e))
    #
    #     headers = [cell.value for cell in next(sheet.iter_rows(min_row=1, max_row=1))]
    #     header_map = {str(h).lower(): i for i, h in enumerate(headers) if h}
    #
    #     required = ['code', 'serial', 'quantity']
    #     for col in required:
    #         if col not in header_map:
    #             raise UserError(_("Missing column '%s' in Excel file.") % col)
    #
    #     not_found_products = []
    #     processed = 0
    #     errors = []
    #
    #     for row in sheet.iter_rows(min_row=2):
    #         code = str(row[header_map['code']].value or '').strip()
    #         serial_name = str(row[header_map['serial']].value or '').strip()
    #         qcell = row[header_map['quantity']].value
    #         try:
    #             quantity = float(qcell or 0)
    #         except Exception:
    #             quantity = 0
    #
    #         if not code:
    #             continue
    #
    #         product = self.env['product.product'].search([('barcode', '=', code)], limit=1)
    #         if not product:
    #             not_found_products.append(code)
    #             continue
    #
    #         move_line = self.picking_id.move_line_ids_without_package.filtered(lambda ml: ml.product_id == product)
    #         move = self.picking_id.move_ids_without_package.filtered(lambda ml: ml.product_id == product)
    #         move.lot_ids=[]
    #         if not move_line:
    #             errors.append(f"Product {code} not found in this Delivery.")
    #             continue
    #
    #         try:
    #             # handle serial-tracked products
    #             if product.tracking == 'serial':
    #                 if not serial_name:
    #                     errors.append(f"Missing serial number for product '{code}'.")
    #                     continue
    #
    #                 lot = self.env['stock.lot'].search([
    #                     ('name', '=', serial_name),
    #                     ('product_id', '=', product.id)
    #                 ], limit=1)
    #
    #                 if not lot:
    #                     if self.picking_id.picking_type_code =='incoming':
    #                         lot = self.env['stock.lot'].create({
    #                             'name': serial_name,
    #                             'product_id': product.id,
    #                             'company_id': self.picking_id.company_id.id,
    #                         })
    #                     else:
    #                         errors.append(f"Serial '{serial_name}' not found for product '{code}'.")
    #                         continue
    #
    #                 existing_line = move_line.filtered(lambda ml: ml.lot_id == lot)
    #                 if existing_line:
    #                     line = existing_line[0]
    #                 else:
    #
    #                     line = move_line[0].copy({
    #                         'lot_id': lot.id,
    #                         'lot_name': lot.name,
    #                         'qty_done': 1.0,
    #                     })
    #                     move_line[0].unlink()
    #
    #             else:
    #                 # for non-serial products
    #                 if quantity <= 0:
    #                     errors.append(f"Invalid quantity for product '{code}'.")
    #                     continue
    #
    #                 line = move_line[0]
    #                 line.qty_done = quantity
    #
    #             processed += 1
    #         except Exception as e:
    #             errors.append(f"Error processing product '{code}': {str(e)}")
    #
    #     # show missing products in one message (do not stop the process)
    #     result_msgs = []
    #     result_msgs.append(f"Upload Completed. Processed lines: {processed}")
    #     if not_found_products:
    #         result_msgs.append("Products not found in system:")
    #         result_msgs.extend(not_found_products)
    #         raise UserError("\n".join(result_msgs))
    #     if errors:
    #         result_msgs.append("Errors:")
    #         result_msgs.extend(errors)
    #         raise UserError("\n".join(result_msgs))
    #
    #     # auto confirm delivery is now moved to separate function and will be called if requested
    #     if self.auto_confirm:
    #         try:
    #             self.action_auto_confirm()
    #         except UserError as e:
    #             result_msgs.append(f"Auto-confirm failed: {e.name if hasattr(e,'name') else str(e)}")
    #
    #     # raise a user friendly message with summary
    #     return {
    #         "type": "ir.actions.client",
    #         "tag": "reload",
    #     }
    def action_upload_excel(self):
        from collections import defaultdict
        from io import BytesIO
        import base64
        import openpyxl
        from odoo.exceptions import UserError
        from odoo.tools.translate import _

        if not self.file:
            raise UserError(_("Please upload an Excel file."))

        # =============================
        # Load Excel
        # =============================
        try:
            data = base64.b64decode(self.file)
            wb = openpyxl.load_workbook(BytesIO(data), data_only=True, read_only=True)
            sheet = wb.active
        except Exception as e:
            raise UserError(_("Invalid Excel file.\nError: %s") % str(e))

        # =============================
        # Headers
        # =============================
        headers = [str(c.value).strip().lower() for c in next(sheet.iter_rows(max_row=1))]
        header_map = {h: i for i, h in enumerate(headers) if h}

        for col in ('code', 'serial', 'quantity'):
            if col not in header_map:
                raise UserError(_("Missing column '%s' in Excel file.") % col)

        # =============================
        # Read rows
        # =============================
        rows = []
        barcodes = set()
        excel_serials = set()

        for r in sheet.iter_rows(min_row=2, values_only=True):
            code = str(r[header_map['code']] or '').strip()
            if not code:
                continue

            serial = str(r[header_map['serial']] or '').strip()
            qty = r[header_map['quantity']] or 0

            rows.append((code, serial, qty))
            barcodes.add(code)
            if serial:
                excel_serials.add(serial)

        if not rows:
            return {"type": "ir.actions.client", "tag": "reload"}

        picking = self.picking_id

        # =============================
        # Batch fetch
        # =============================
        products = self.env['product.product'].search([('barcode', 'in', list(barcodes))])
        product_map = {p.barcode: p for p in products}

        moves = picking.move_ids_without_package
        move_map = {m.product_id.id: m for m in moves}

        move_lines = picking.move_line_ids_without_package
        move_line_map = defaultdict(list)
        for ml in move_lines:
            move_line_map[ml.product_id.id].append(ml)

        lots = self.env['stock.lot'].sudo().search([
            ('name', 'in', list(excel_serials)),
            ('product_id', 'in', products.ids)
        ])
        lot_map = {(l.product_id.id, l.name): l for l in lots}

        # =============================
        # Existing serials in picking
        # =============================
        existing_serials = set(
            picking.move_line_ids_without_package
            .filtered(lambda l: l.lot_id)
            .mapped(lambda l: (l.product_id.id, l.lot_id.name))
        )

        # =============================
        # Collect data (NO duplication)
        # =============================
        qty_map = defaultdict(float)  # product_id -> total qty
        serial_map = defaultdict(set)  # product_id -> set(serials)

        not_found_products = []
        errors = []

        for code, serial_name, quantity in rows:
            product = product_map.get(code)
            if not product:
                not_found_products.append(code)
                continue

            if product.tracking == 'serial':
                if not serial_name:
                    errors.append(f"Missing serial for product '{code}'.")
                    continue
                serial_map[product.id].add(serial_name)
            else:
                try:
                    qty_map[product.id] += float(quantity or 0)
                except Exception:
                    errors.append(f"Invalid quantity for product '{code}'.")

        processed = 0

        # =============================
        # Process non-serial products
        # =============================
        for product_id, total_qty in qty_map.items():
            if total_qty <= 0:
                continue

            lines = move_line_map.get(product_id)
            if not lines:
                errors.append(f"Product not found in picking.")
                continue

            lines[0].qty_done = total_qty
            processed += 1

        # =============================
        # Process serial products
        # =============================
        for product_id, serials in serial_map.items():
            product = self.env['product.product'].browse(product_id)
            move = move_map.get(product_id)

            if not move:
                errors.append(f"Product '{product.display_name}' not found in picking.")
                continue

            for serial_name in serials:
                key = (product_id, serial_name)

                # 🔒 Prevent duplicate serial in picking
                if key in existing_serials:
                    # errors.append(
                    #     f"Serial '{serial_name}' already exists in picking "
                    #     f"for '{product.display_name}'."
                    # )
                    continue

                lot = lot_map.get(key)
                if not lot:
                    if picking.picking_type_code == 'incoming':
                        lot = self.env['stock.lot'].create({
                            'name': serial_name,
                            'product_id': product_id,

                        })
                        lot_map[key] = lot
                    else:
                        errors.append(
                            f"Serial '{serial_name}' not found for '{product.display_name}'."
                        )
                        continue

                self.env['stock.move.line'].create({
                    'move_id': move.id,
                    'picking_id': picking.id,
                    'product_id': product_id,
                    'lot_id': lot.id,
                    'qty_done': 1.0,
                    'location_id': move.location_id.id,
                    'location_dest_id': move.location_dest_id.id,
                })

                existing_serials.add(key)
                processed += 1

        # =============================
        # Results
        # =============================
        messages = [f"Upload completed. Processed lines: {processed}"]

        if not_found_products:
            messages.append("Products not found:")
            messages.extend(sorted(set(not_found_products)))

        if errors:
            messages.append("Errors:")
            messages.extend(errors)
            raise UserError("\n".join(messages))

        if self.auto_confirm:
            self.action_auto_confirm()

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

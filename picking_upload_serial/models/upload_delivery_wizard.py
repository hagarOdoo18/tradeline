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
        is_incoming = picking.picking_type_code == 'incoming'

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
        qty_map = defaultdict(float)   # product_id -> total qty
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
        #   PERFORMANCE: batch the lot creation and the move-line creation
        #   into single create() calls instead of one create() per serial.
        # =============================

        # ---- Pass 1: figure out which lots are missing and must be created ----
        missing_lot_vals = []
        missing_lot_keys = []
        seen_missing = set()

        for product_id, serials in serial_map.items():
            for serial_name in serials:
                key = (product_id, serial_name)
                if key in existing_serials or key in lot_map:
                    continue
                if is_incoming:
                    if key in seen_missing:
                        continue
                    seen_missing.add(key)
                    missing_lot_vals.append({
                        'name': serial_name,
                        'product_id': product_id,
                    })
                    missing_lot_keys.append(key)

        # ---- Batch-create the missing lots (single DB call) ----
        if missing_lot_vals:
            new_lots = self.env['stock.lot'].create(missing_lot_vals)
            for key, lot in zip(missing_lot_keys, new_lots):
                lot_map[key] = lot

        # ---- Pass 2: build all move-line values, then create in one call ----
        move_line_vals = []

        for product_id, serials in serial_map.items():
            move = move_map.get(product_id)
            if not move:
                product = self.env['product.product'].browse(product_id)
                errors.append(f"Product '{product.display_name}' not found in picking.")
                continue

            move_location_id = move.location_id.id
            move_dest_id = move.location_dest_id.id

            for serial_name in serials:
                key = (product_id, serial_name)

                # prevent duplicate serial in picking
                if key in existing_serials:
                    continue

                lot = lot_map.get(key)
                if not lot:
                    product = self.env['product.product'].browse(product_id)
                    errors.append(
                        f"Serial '{serial_name}' not found for '{product.display_name}'."
                    )
                    continue

                move_line_vals.append({
                    'move_id': move.id,
                    'picking_id': picking.id,
                    'product_id': product_id,
                    'lot_id': lot.id,
                    'qty_done': 1.0,
                    'location_id': move_location_id,
                    'location_dest_id': move_dest_id,
                })

                existing_serials.add(key)
                processed += 1

        # ---- Batch-create every move line (single DB call) ----
        if move_line_vals:
            self.env['stock.move.line'].create(move_line_vals)

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

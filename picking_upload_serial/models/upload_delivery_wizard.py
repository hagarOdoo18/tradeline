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

        self.ensure_one()

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

        # NOTE: the same product can sit on SEVERAL moves of one picking,
        # so keep every move per product instead of only the last one.
        moves_by_product = defaultdict(lambda: self.env['stock.move'])
        for mv in picking.move_ids_without_package:
            moves_by_product[mv.product_id.id] |= mv

        lots = self.env['stock.lot'].sudo().search([
            ('name', 'in', list(excel_serials)),
            ('product_id', 'in', products.ids),
            ('company_id', 'in', [picking.company_id.id, False]),
        ])
        lot_map = {(l.product_id.id, l.name): l for l in lots}

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
                    errors.append(_("Missing serial for product '%s'.") % code)
                    continue
                serial_map[product.id].add(serial_name)
            else:
                try:
                    qty_map[product.id] += float(quantity or 0)
                except Exception:
                    errors.append(_("Invalid quantity for product '%s'.") % code)

        MoveLine = self.env['stock.move.line']
        processed = 0

        # =============================================================
        # NON-serial products
        #
        # Odoo 17/18: stock.move.line no longer has `qty_done`; the done
        # quantity is `quantity` (+ `picked`). Writing `qty_done` on a
        # record is a silent no-op, so the reservation lines kept their
        # own quantity and the uploaded quantity was added on top of it
        # -> the doubled quantity on the move.
        # We now OVERWRITE the existing line instead of adding a new one.
        # =============================================================
        for product_id, total_qty in qty_map.items():
            if total_qty <= 0:
                continue

            moves = moves_by_product.get(product_id)
            if not moves:
                product = self.env['product.product'].browse(product_id)
                errors.append(_("Product '%s' not found in picking.") % product.display_name)
                continue

            move = moves[0]
            lines = move.move_line_ids

            if lines:
                # keep exactly ONE line: the move quantity then equals the
                # file, never file + reservation.
                lines[0].write({'quantity': total_qty, 'picked': True})
                if len(lines) > 1:
                    lines[1:].unlink()
            else:
                MoveLine.create({
                    'move_id': move.id,
                    'picking_id': picking.id,
                    'product_id': product_id,
                    'product_uom_id': move.product_uom.id,
                    'quantity': total_qty,
                    'picked': True,
                    'location_id': move.location_id.id,
                    'location_dest_id': move.location_dest_id.id,
                })

            # other moves of the same product must not add quantity again
            for extra_move in moves[1:]:
                extra_move.move_line_ids.unlink()

            processed += 1

        # =============================================================
        # SERIAL products
        # =============================================================

        # ---- Pass 1: create the missing lots (single DB call) ----
        missing_lot_vals = []
        missing_lot_keys = []

        if is_incoming and self.create_if_not_exist:
            for product_id, serials in serial_map.items():
                for serial_name in serials:
                    key = (product_id, serial_name)
                    if key in lot_map:
                        continue
                    missing_lot_keys.append(key)
                    missing_lot_vals.append({
                        'name': serial_name,
                        'product_id': product_id,
                        'company_id': picking.company_id.id,
                    })

        if missing_lot_vals:
            new_lots = self.env['stock.lot'].create(missing_lot_vals)
            for key, lot in zip(missing_lot_keys, new_lots):
                lot_map[key] = lot

        # ---- Pass 2: exactly one move line per serial.
        # We reuse the empty lines Odoo created when the picking was
        # reserved and delete the leftovers; that is what stops the
        # quantity from being counted twice on the move. ----
        move_line_vals = []

        for product_id, serials in serial_map.items():
            moves = moves_by_product.get(product_id)
            if not moves:
                product = self.env['product.product'].browse(product_id)
                errors.append(_("Product '%s' not found in picking.") % product.display_name)
                continue

            move = moves[0]
            all_lines = moves.move_line_ids

            # serials already sitting on the picking for this product
            already_there = {ml.lot_id.name for ml in all_lines if ml.lot_id}

            # empty reservation lines we can recycle
            free_lines = [ml for ml in all_lines if not ml.lot_id]

            # existing serial lines must count exactly 1 each
            for ml in all_lines:
                if ml.lot_id and (ml.quantity != 1.0 or not ml.picked):
                    ml.write({'quantity': 1.0, 'picked': True})

            new_lots = []
            for serial_name in sorted(serials):
                if serial_name in already_there:
                    continue  # already on the picking -> never add it twice

                lot = lot_map.get((product_id, serial_name))
                if not lot:
                    product = self.env['product.product'].browse(product_id)
                    errors.append(
                        _("Serial '%s' not found for '%s'.") % (serial_name, product.display_name)
                    )
                    continue
                new_lots.append(lot)

            for lot in new_lots:
                if free_lines:
                    ml = free_lines.pop(0)
                    ml.write({'lot_id': lot.id, 'quantity': 1.0, 'picked': True})
                else:
                    move_line_vals.append({
                        'move_id': move.id,
                        'picking_id': picking.id,
                        'product_id': product_id,
                        'product_uom_id': move.product_uom.id,
                        'lot_id': lot.id,
                        'quantity': 1.0,
                        'picked': True,
                        'location_id': move.location_id.id,
                        'location_dest_id': move.location_dest_id.id,
                    })
                processed += 1

            # drop the reservation lines nobody used, otherwise they keep
            # adding their own quantity on top of the uploaded serials
            for ml in free_lines:
                ml.unlink()

        if move_line_vals:
            MoveLine.create(move_line_vals)

        # =============================
        # Results
        # =============================
        messages = [_("Upload completed. Processed lines: %s") % processed]

        if not_found_products:
            messages.append(_("Products not found:"))
            messages.extend(sorted(set(not_found_products)))

        if errors:
            messages.append(_("Errors:"))
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

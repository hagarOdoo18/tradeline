from odoo import models, fields
from odoo.exceptions import UserError
import base64, xlrd
import base64
import openpyxl
from io import BytesIO
class ImportTransferExcel(models.TransientModel):
    _name = "import.transfer.excel"
    _description = "Import Excel with Preview (Odoo 18)"

    file = fields.Binary(string="Excel File", required=True)
    filename = fields.Char()
    preview_line_ids = fields.One2many(
        "import.transfer.excel.line", "wizard_id", string="Preview"
    )
    picking_id = fields.Many2one(
        comodel_name='stock.picking',
        string='Picking',
        required=False)

    def action_preview(self):
        self.preview_line_ids.unlink()
        self.picking_id = self.env.context.get("active_id")
        if not self.picking_id:
            raise UserError("No active transfer")

        data = base64.b64decode(self.file)
        wb = openpyxl.load_workbook(filename=BytesIO(data), data_only=True)
        sheet = wb.active

        merged = {}
        for row in sheet.iter_rows(min_row=2, values_only=True):
            code = str(row[0]).strip() if row[0] else ''
            qty = float(row[1] or 0)

            if code and qty > 0:
                merged[code] = merged.get(code, 0) + qty

        for code, qty in merged.items():
            product = self.env["product.product"].search(
                [("barcode", "=", code)], limit=1
            )
            self.env["import.transfer.excel.line"].create({
                "wizard_id": self.id,
                "itemcode": code,
                "quantity": qty,
                "product_id": product.id if product else False,
            })

        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    def action_import(self):
        picking = self.picking_id

        for line in self.preview_line_ids:
            if not line.product_id:
                raise UserError(f"Product not found: {line.itemcode}")

            move = picking.move_ids.filtered(
                lambda m: m.product_id == line.product_id and m.state != "cancel"
            )
            if move:
                move.product_uom_qty = line.quantity
                move.move_line_ids.qty_done = line.quantity
            else:
                move = self.env["stock.move"].create({
                    "name": line.product_id.display_name,
                    "product_id": line.product_id.id,
                    "product_uom_qty": line.quantity,
                    "product_uom": line.product_id.uom_id.id,
                    "picking_id": picking.id,
                    "location_id": picking.location_id.id,
                    "location_dest_id": picking.location_dest_id.id,
                })
                move._action_confirm()
                self.env["stock.move.line"].create({
                    "move_id": move.id,
                    "picking_id": picking.id,
                    "product_id": line.product_id.id,
                    "product_uom_id": line.product_id.uom_id.id,
                    "qty_done": line.quantity,
                    "location_id": picking.location_id.id,
                    "location_dest_id": picking.location_dest_id.id,
                })

        return {"type": "ir.actions.act_window_close"}


class ImportTransferExcelLine(models.TransientModel):
    _name = "import.transfer.excel.line"
    _description = "Import Excel Preview Line"

    wizard_id = fields.Many2one("import.transfer.excel", ondelete="cascade")
    itemcode = fields.Char()
    product_id = fields.Many2one("product.product")
    quantity = fields.Float()
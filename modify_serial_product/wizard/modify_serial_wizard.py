# -*- coding: utf-8 -*-
import base64
import logging
from io import BytesIO

import openpyxl

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ModifySerialWizard(models.TransientModel):
    """Wizard to reassign serial/lot numbers from one product to another.

    Reads an Excel file with three columns:
        A  Item Code          - barcode of the *current* product on the lot
        B  Lot/Serial Number  - the lot/serial name to move
        C  new_item_code      - barcode of the *target* product

    Approach - stock.quant._update_available_quantity():
        Odoo 18 blocks direct stock.quant creation (even with inventory_mode=True).
        _update_available_quantity() is the internal core API used by Odoo itself
        when validating stock moves - it creates or updates quants directly without
        hitting any restriction check.

        1. Find the stock.quant(s) for the serial on the old product.
        2. Call _update_available_quantity(old_product, location, -qty, lot=old_lot)
           to remove the stock.
        3. Create (or find) a new stock.lot with the same name on the new product.
        4. Call _update_available_quantity(new_product, location, +qty, lot=new_lot)
           to add the stock on the correct product.

    The old lot record is left in place for historical traceability.
    """

    _name = 'modify.serial.wizard'
    _description = 'Modify Serial / Lot Product'

    file = fields.Binary(string='Excel File (.xlsx)', required=True)
    file_name = fields.Char(string='File Name')
    state = fields.Selection(
        [('upload', 'Upload'), ('done', 'Done')],
        default='upload',
        readonly=True,
    )
    result_html = fields.Html(string='Result', readonly=True, sanitize=False)

    def action_process(self):
        """Parse the Excel file and reassign each serial/lot to its new product."""
        self.ensure_one()
        if not self.file:
            raise UserError(_('Please upload an Excel file before processing.'))

        try:
            wb = openpyxl.load_workbook(BytesIO(base64.b64decode(self.file)))
        except Exception as exc:
            raise UserError(_('Could not read the Excel file: %s') % exc)

        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            raise UserError(_('The Excel file is empty.'))

        data_rows = rows[1:]  # skip header

        results = []

        Lot = self.env['stock.lot'].sudo()
        Product = self.env['product.product'].sudo()
        Quant = self.env['stock.quant'].sudo()

        for idx, row in enumerate(data_rows, start=2):
            if not any(row):
                continue

            old_code = str(row[0]).strip() if row[0] else ''
            serial_name = str(row[1]).strip() if row[1] else ''
            new_code = str(row[2]).strip() if row[2] else ''

            if not old_code or not serial_name or not new_code:
                results.append((idx, serial_name, old_code, new_code,
                                 'error', 'Missing value in one or more columns'))
                continue

            old_product = Product.search(
                [('barcode', '=', old_code),
                 ('company_id', 'in', [self.env.company.id, False])],
                limit=1,
            )
            if not old_product:
                results.append((idx, serial_name, old_code, new_code,
                                 'error',
                                 'Old product with barcode "%s" not found' % old_code))
                continue

            new_product = Product.search(
                [('barcode', '=', new_code),
                 ('company_id', 'in', [self.env.company.id, False])],
                limit=1,
            )
            if not new_product:
                results.append((idx, serial_name, old_code, new_code,
                                 'error',
                                 'New product with barcode "%s" not found' % new_code))
                continue

            if old_product.id == new_product.id:
                results.append((idx, serial_name, old_code, new_code,
                                 'skip', 'Old and new product are the same - skipped'))
                continue

            old_lot = Lot.search(
                [('name', '=', serial_name),
                 ('product_id', '=', old_product.id),
                 ('company_id', 'in', [self.env.company.id, False])],
                limit=1,
            )
            if not old_lot:
                results.append((idx, serial_name, old_code, new_code,
                                 'error',
                                 'Serial/lot "%s" not found for product "%s"'
                                 % (serial_name, old_code)))
                continue

            quants = Quant.search([
                ('lot_id', '=', old_lot.id),
                ('location_id.usage', '=', 'internal'),
                ('quantity', '>', 0),
            ])
            if not quants:
                results.append((idx, serial_name, old_code, new_code,
                                 'error',
                                 'No available stock for serial "%s" '
                                 '(qty is zero or not in a stock location)'
                                 % serial_name))
                continue

            try:
                # get or create the new lot (same serial name, new product)
                new_lot = Lot.search(
                    [('name', '=', serial_name),
                     ('product_id', '=', new_product.id),
                     ],
                    limit=1,
                )
                if not new_lot:
                    new_lot = Lot.create({
                        'name': serial_name,
                        'product_id': new_product.id,

                    })

                total_qty = 0
                for quant in quants:
                    qty = quant.quantity
                    location = quant.location_id

                    # Remove stock from old product / old lot
                    Quant._update_available_quantity(
                        old_product, location, -qty, lot_id=old_lot)

                    # Add stock to new product / new lot at same location
                    Quant._update_available_quantity(
                        new_product, location, qty, lot_id=new_lot)

                    total_qty += qty

                results.append((idx, serial_name, old_code, new_code,
                                 'ok',
                                 'Reassigned %.0f unit(s) to new product'
                                 % total_qty))

            except Exception as exc:
                _logger.exception(
                    'modify_serial_product: error processing lot %s', serial_name)
                results.append((idx, serial_name, old_code, new_code,
                                 'error', str(exc)))

        ok_count = sum(1 for r in results if r[4] == 'ok')
        skip_count = sum(1 for r in results if r[4] == 'skip')
        err_count = sum(1 for r in results if r[4] == 'error')

        STATUS_STYLE = {
            'ok':    'background:#d4edda;color:#155724;',
            'skip':  'background:#fff3cd;color:#856404;',
            'error': 'background:#f8d7da;color:#721c24;',
        }
        STATUS_LABEL = {'ok': 'OK', 'skip': 'Skipped', 'error': 'Error'}

        rows_html = ''
        for (row_no, serial, old_c, new_c, status, msg) in results:
            style = STATUS_STYLE.get(status, '')
            label = STATUS_LABEL.get(status, status)
            td = 'padding:4px 8px;border:1px solid #ddd;'
            rows_html += (
                '<tr style="%s">'
                '<td style="%s">%s</td>'
                '<td style="%s">%s</td>'
                '<td style="%s">%s</td>'
                '<td style="%s">%s</td>'
                '<td style="%s font-weight:bold;">%s</td>'
                '<td style="%s">%s</td>'
                '</tr>'
            ) % (style, td, row_no, td, serial,
                 td, old_c, td, new_c, td, label, td, msg)

        hdr = 'padding:4px 8px;border:1px solid #ddd;'
        html = (
            '<div style="font-family:sans-serif;">'
            '<p style="margin:0 0 8px 0;"><b>Processed %d row(s):</b> '
            '<span style="color:#155724;">%d OK</span> &nbsp;'
            '<span style="color:#856404;">%d Skipped</span> &nbsp;'
            '<span style="color:#721c24;">%d Error(s)</span></p>'
            '<table style="border-collapse:collapse;width:100%%;font-size:13px;">'
            '<thead><tr style="background:#f2f2f2;">'
            '<th style="%s">Row</th>'
            '<th style="%s">Serial/Lot</th>'
            '<th style="%s">Old Barcode</th>'
            '<th style="%s">New Barcode</th>'
            '<th style="%s">Status</th>'
            '<th style="%s">Message</th>'
            '</tr></thead>'
            '<tbody>%s</tbody>'
            '</table></div>'
        ) % (len(results), ok_count, skip_count, err_count,
             hdr, hdr, hdr, hdr, hdr, hdr, rows_html)

        self.write({'state': 'done', 'result_html': html})

        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

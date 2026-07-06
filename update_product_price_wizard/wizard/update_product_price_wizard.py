# -*- coding: utf-8 -*-
import base64
import io
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

try:
    import openpyxl
except ImportError:
    openpyxl = None

try:
    import xlrd
except ImportError:
    xlrd = None


class UpdateProductPriceWizard(models.TransientModel):
    _name = 'update.product.price.wizard'
    _description = 'Update Product Price Wizard'

    # ── File upload ────────────────────────────────────────────────────────
    import_file = fields.Binary(
        string='Excel File',
        required=True,
        help='Upload an Excel (.xlsx / .xls) file with two columns:\n'
             '  • Item Code  – product internal reference or barcode\n'
             '  • New Price  – new sales price',
    )
    import_file_name = fields.Char(string='File Name')

    # ── State / summary ────────────────────────────────────────────────────
    state = fields.Selection(
        selection=[('draft', 'Draft'), ('preview', 'Preview'), ('done', 'Done')],
        default='draft',
        readonly=True,
    )
    import_summary = fields.Char(string='Summary', readonly=True)

    # ── Preview lines ──────────────────────────────────────────────────────
    line_ids = fields.One2many(
        'update.product.price.wizard.line',
        'wizard_id',
        string='Lines',
        readonly=True,
    )

    # ──────────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────────

    def _normalize_header(self, raw):
        return (raw or '').strip().lower().replace(' ', '_')

    def _parse_file(self):
        """Return list of dicts: [{'item_code': str, 'new_price': float}]."""
        self.ensure_one()
        if not self.import_file:
            raise UserError(_('Please upload a file.'))

        file_bytes = base64.b64decode(self.import_file)
        fname = (self.import_file_name or '').lower()

        if fname.endswith('.xlsx'):
            return self._parse_xlsx(file_bytes)
        elif fname.endswith('.xls'):
            return self._parse_xls(file_bytes)
        else:
            # try xlsx then xls
            for parser in (self._parse_xlsx, self._parse_xls):
                try:
                    return parser(file_bytes)
                except Exception:
                    pass
            raise UserError(
                _('Unsupported file format. Please upload a .xlsx or .xls file.')
            )

    def _parse_xlsx(self, file_bytes):
        if not openpyxl:
            raise UserError(
                _('Python library "openpyxl" is not installed. '
                  'Ask your administrator to install it.')
            )
        wb = openpyxl.load_workbook(
            io.BytesIO(file_bytes), read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        wb.close()
        if not rows:
            raise UserError(_('The uploaded file is empty.'))
        return self._rows_to_dicts(rows)

    def _parse_xls(self, file_bytes):
        if not xlrd:
            raise UserError(
                _('Python library "xlrd" is not installed. '
                  'Ask your administrator to install it.')
            )
        wb = xlrd.open_workbook(file_contents=file_bytes)
        ws = wb.sheet_by_index(0)
        rows = [
            tuple(ws.cell_value(r, c) for c in range(ws.ncols))
            for r in range(ws.nrows)
        ]
        if not rows:
            raise UserError(_('The uploaded file is empty.'))
        return self._rows_to_dicts(rows)

    def _rows_to_dicts(self, rows):
        headers = [self._normalize_header(str(h)) for h in rows[0]]

        code_aliases = {
            'item_code', 'itemcode', 'item code',
            'internal_reference', 'default_code',
            'product_code', 'code', 'ref', 'reference', 'barcode',
        }
        price_aliases = {
            'new_price', 'newprice', 'new price',
            'price', 'unit_price', 'sales_price',
            'list_price', 'sale_price',
        }

        # Normalize aliases the same way we normalize headers
        code_aliases = {self._normalize_header(a) for a in code_aliases}
        price_aliases = {self._normalize_header(a) for a in price_aliases}

        code_col = next(
            (i for i, h in enumerate(headers) if h in code_aliases), None)
        price_col = next(
            (i for i, h in enumerate(headers) if h in price_aliases), None)

        if code_col is None:
            raise UserError(
                _('Column "Item Code" not found in the file.\n'
                  'Detected headers: %s') % ', '.join(headers)
            )
        if price_col is None:
            raise UserError(
                _('Column "New Price" not found in the file.\n'
                  'Detected headers: %s') % ', '.join(headers)
            )

        data = []
        for row_num, row in enumerate(rows[1:], start=2):
            if len(row) <= max(code_col, price_col):
                continue
            code = str(row[code_col]).strip() if row[code_col] is not None else ''
            price_raw = row[price_col]
            if not code or code.lower() in ('false', 'none', ''):
                continue
            try:
                price = float(str(price_raw).strip().replace(',', '.'))
            except (ValueError, TypeError):
                raise UserError(
                    _('Row %d: invalid price value "%s" for item code "%s".')
                    % (row_num, price_raw, code)
                )
            data.append({'item_code': code, 'new_price': price})

        if not data:
            raise UserError(_('No valid data rows found in the file.'))
        return data

    def _find_product(self, item_code):
        """Look up product.template by default_code first, then barcode."""
        Product = self.env['product.product']

        product = Product.search(
                [('barcode', '=', item_code)], limit=1)
        return product

    # ──────────────────────────────────────────────────────────────────────
    # Button actions
    # ──────────────────────────────────────────────────────────────────────

    def action_preview(self):
        """Parse file and populate preview lines."""
        self.ensure_one()
        rows = self._parse_file()

        preview_vals = []
        for row in rows:
            product = self._find_product(row['item_code'])
            preview_vals.append({
                'wizard_id': self.id,
                'item_code': row['item_code'],
                'new_price': row['new_price'],
                'old_price': product.list_price if product else 0.0,
                'product_id': product.id if product else False,
                'product_name': product.name if product else _('— Not Found —'),
                'status': 'ok' if product else 'error',
            })

        self.line_ids.unlink()
        self.env['update.product.price.wizard.line'].create(preview_vals)

        ok = sum(1 for v in preview_vals if v['status'] == 'ok')
        err = len(preview_vals) - ok
        self.write({
            'state': 'preview',
            'import_summary': _(
                '%d products ready to update, %d not found') % (ok, err),
        })
        return self._reopen()

    def action_apply(self):
        """Write new_price → list_price for all valid lines."""
        self.ensure_one()
        valid = self.line_ids.filtered(
            lambda l: l.status == 'ok' and l.product_id)
        if not valid:
            raise UserError(
                _('No valid products to update. '
                  'Fix the errors shown in the preview and try again.'))

        updated = 0
        for line in valid:
            line.product_id.write({'list_price': line.new_price})
            updated += 1

        self.write({
            'state': 'done',
            'import_summary': _('%d product price(s) updated successfully.') % updated,
        })
        return self._reopen()

    def action_reset(self):
        self.line_ids.unlink()
        self.write({
            'state': 'draft',
            'import_summary': False,
            'import_file': False,
            'import_file_name': False,
        })
        return self._reopen()

    def action_close(self):
        return {'type': 'ir.actions.act_window_close'}

    def _reopen(self):
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }


class UpdateProductPriceWizardLine(models.TransientModel):
    _name = 'update.product.price.wizard.line'
    _description = 'Update Product Price Wizard – Line'

    wizard_id = fields.Many2one(
        'update.product.price.wizard', ondelete='cascade')
    item_code = fields.Char(string='Item Code', readonly=True)
    product_id = fields.Many2one(
        'product.product', string='Product', readonly=True)
    product_name = fields.Char(string='Product Name', readonly=True)
    old_price = fields.Float(
        string='Current Price', digits='Product Price', readonly=True)
    new_price = fields.Float(
        string='New Price', digits='Product Price', readonly=True)
    status = fields.Selection(
        [('ok', 'Ready'), ('error', 'Not Found')],
        string='Status',
        readonly=True,
    )

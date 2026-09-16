import base64
import io
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

try:
    import xlrd
except ImportError:
    xlrd = None

try:
    import openpyxl
except ImportError:
    openpyxl = None

try:
    import csv
except ImportError:
    csv = None


class PricelistImportWizard(models.TransientModel):
    _name = 'pricelist.import.wizard'
    _description = 'Pricelist Import Wizard'

    # ── Pricelist context ──────────────────────────────────────────────────
    pricelist_id = fields.Many2one(
        'product.pricelist',
        string='Pricelist',
        required=True,
        readonly=True,
    )
    currency_id = fields.Many2one(
        related='pricelist_id.currency_id',
        string='Currency',
        readonly=True,
    )
    company_id = fields.Many2one(
        related='pricelist_id.company_id',
        string='Company',
        readonly=True,
    )

    # ── Import file ────────────────────────────────────────────────────────
    import_file = fields.Binary(
        string='Import File',
        required=True,
        help='Upload an Excel (.xlsx / .xls) or CSV file with columns:\n'
             '  • item_code  – product internal reference\n'
             '  • fixed_price – price value',
    )
    import_file_name = fields.Char(string='File Name')

    # ── Options ────────────────────────────────────────────────────────────
    applied_on = fields.Selection(
        selection=[
            ('3_global', 'All Products'),
            ('2_product_category', 'Product Category'),
            ('1_product', 'Product'),
            ('0_product_variant', 'Product Variant'),
        ],
        string='Apply On',
        default='1_product',
        required=True,
        readonly=True,
        help='Fixed to "Product" level as per import requirements.',
    )
    compute_price = fields.Selection(
        selection=[
            ('fixed', 'Fixed Price'),
            ('percentage', 'Discount'),
            ('formula', 'Formula'),
        ],
        string='Compute Price',
        default='fixed',
        required=True,
        readonly=True,
        help='Fixed to "Fixed Price" as per import requirements.',
    )
    date_start = fields.Date(string='Start Date',        required=True,)
    date_end = fields.Date(string='End Date'    ,    required=True,)
    overwrite_existing = fields.Boolean(
        string='Overwrite Existing Items',
        default=True,
        help='If checked, existing pricelist items for the same product '
             'will be updated instead of creating duplicates.',
    )

    # ── Preview / result ───────────────────────────────────────────────────
    preview_line_ids = fields.One2many(
        'pricelist.import.wizard.line',
        'wizard_id',
        string='Preview Lines',
        readonly=True,
    )
    state = fields.Selection(
        selection=[('draft', 'Draft'), ('preview', 'Preview'), ('done', 'Done')],
        default='draft',
        readonly=True,
    )
    import_summary = fields.Char(string='Import Summary', readonly=True)

    # ──────────────────────────────────────────────────────────────────────
    # Default / onchange
    # ──────────────────────────────────────────────────────────────────────

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        active_id = self.env.context.get('active_id')
        if active_id:
            res['pricelist_id'] = active_id
        return res

    # ──────────────────────────────────────────────────────────────────────
    # File parsing helpers
    # ──────────────────────────────────────────────────────────────────────

    def _parse_file(self):
        """Return a list of dicts: [{'item_code': ..., 'fixed_price': ...}]."""
        self.ensure_one()
        if not self.import_file:
            raise UserError(_('Please upload a file before continuing.'))

        file_name = (self.import_file_name or '').lower()
        file_bytes = base64.b64decode(self.import_file)

        if file_name.endswith('.csv'):
            return self._parse_csv(file_bytes)
        elif file_name.endswith('.xlsx'):
            return self._parse_xlsx(file_bytes)
        elif file_name.endswith('.xls'):
            return self._parse_xls(file_bytes)
        else:
            # Try xlsx first, then csv
            try:
                return self._parse_xlsx(file_bytes)
            except Exception:
                pass
            try:
                return self._parse_csv(file_bytes)
            except Exception:
                pass
            raise UserError(
                _('Unsupported file format. Please upload a .xlsx, .xls, or .csv file.')
            )

    def _normalize_header(self, raw):
        return (raw or '').strip().lower().replace(' ', '_')

    def _parse_xlsx(self, file_bytes):
        if not openpyxl:
            raise UserError(
                _('Python library "openpyxl" is required to read .xlsx files. '
                  'Ask your system administrator to install it.')
            )
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            raise UserError(_('The file is empty.'))
        return self._rows_to_dicts(rows)

    def _parse_xls(self, file_bytes):
        if not xlrd:
            raise UserError(
                _('Python library "xlrd" is required to read .xls files. '
                  'Ask your system administrator to install it.')
            )
        wb = xlrd.open_workbook(file_contents=file_bytes)
        ws = wb.sheet_by_index(0)
        rows = [
            tuple(ws.cell_value(r, c) for c in range(ws.ncols))
            for r in range(ws.nrows)
        ]
        if not rows:
            raise UserError(_('The file is empty.'))
        return self._rows_to_dicts(rows)

    def _parse_csv(self, file_bytes):
        import csv as csv_mod
        text = file_bytes.decode('utf-8-sig', errors='replace')
        reader = csv_mod.reader(io.StringIO(text))
        rows = [tuple(row) for row in reader if any(cell.strip() for cell in row)]
        if not rows:
            raise UserError(_('The file is empty.'))
        return self._rows_to_dicts(rows)

    def _rows_to_dicts(self, rows):
        headers = [self._normalize_header(str(h)) for h in rows[0]]

        # Accept flexible column names
        code_aliases = {'item_code', 'internal_reference', 'default_code',
                        'product_code', 'code', 'ref', 'reference'}
        price_aliases = {'fixed_price', 'price', 'unit_price', 'sales_price',
                         'list_price', 'sale_price'}

        code_col = next((i for i, h in enumerate(headers) if h in code_aliases), None)
        price_col = next((i for i, h in enumerate(headers) if h in price_aliases), None)

        if code_col is None:
            raise UserError(
                _('Column "item_code" not found.\n'
                  'Accepted names: %s\n'
                  'Detected headers: %s') % (
                    ', '.join(sorted(code_aliases)),
                    ', '.join(headers),
                )
            )
        if price_col is None:
            raise UserError(
                _('Column "fixed_price" not found.\n'
                  'Accepted names: %s\n'
                  'Detected headers: %s') % (
                    ', '.join(sorted(price_aliases)),
                    ', '.join(headers),
                )
            )

        data = []
        for row_idx, row in enumerate(rows[1:], start=2):
            if len(row) <= max(code_col, price_col):
                continue
            code = str(row[code_col]).strip() if row[code_col] is not None else ''
            price_raw = row[price_col]
            if not code:
                continue
            try:
                price = float(str(price_raw).strip().replace(',', '.'))
            except (ValueError, TypeError):
                raise UserError(
                    _('Row %d: invalid price value "%s" for item_code "%s".')
                    % (row_idx, price_raw, code)
                )
            data.append({'item_code': code, 'fixed_price': price})

        if not data:
            raise UserError(_('No valid data rows found in the file.'))
        return data

    # ──────────────────────────────────────────────────────────────────────
    # Actions
    # ──────────────────────────────────────────────────────────────────────

    def action_preview(self):
        """Parse file and populate preview lines."""
        self.ensure_one()
        rows = self._parse_file()

        # Resolve products
        ProductTemplate = self.env['product.template']
        preview_vals = []
        errors = []

        for row in rows:
            product = ProductTemplate.search(
                [('barcode', '=', row['item_code'])], limit=1
            )
            status = 'ok' if product else 'error'
            error_msg = '' if product else _('Product not found')
            preview_vals.append({
                'wizard_id': self.id,
                'item_code': row['item_code'],
                'fixed_price': row['fixed_price'],
                'product_tmpl_id': product.id if product else False,
                'product_name': product.name if product else _('— Not Found —'),
                'status': status,
                'error_message': error_msg,
            })
            if not product:
                errors.append(row['item_code'])

        # Clear existing preview lines
        self.preview_line_ids.unlink()
        self.env['pricelist.import.wizard.line'].create(preview_vals)

        ok_count = len(preview_vals) - len(errors)
        summary = _('%d rows ready to import, %d errors (product not found)') % (
            ok_count, len(errors)
        )
        self.write({'state': 'preview', 'import_summary': summary})

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'pricelist.import.wizard',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_import(self):
        """Create / update pricelist items for all valid preview lines."""
        self.ensure_one()
        valid_lines = self.preview_line_ids.filtered(
            lambda l: l.status == 'ok' and l.product_tmpl_id
        )
        if not valid_lines:
            raise UserError(
                _('No valid rows to import. '
                  'Please fix the errors shown in the preview and try again.')
            )

        PricelistItem = self.env['product.pricelist.item']
        created = updated = 0

        for line in valid_lines:
            domain = [
                ('pricelist_id', '=', self.pricelist_id.id),
                ('applied_on', '=', '1_product'),
                ('product_tmpl_id', '=', line.product_tmpl_id.id),
            ]
            vals = {
                'pricelist_id': self.pricelist_id.id,
                'applied_on': '1_product',
                'compute_price': 'fixed',
                'fixed_price': line.fixed_price,
                'product_tmpl_id': line.product_tmpl_id.id,
                'currency_id': self.pricelist_id.currency_id.id,
                'date_start': self.date_start or False,
                'date_end': self.date_end or False,
            }

            existing = PricelistItem.search(domain, limit=1)
            if existing and self.overwrite_existing:
                existing.write(vals)
                updated += 1
            elif not existing:
                PricelistItem.create(vals)
                created += 1

        summary = _('%d items created, %d items updated.') % (created, updated)
        self.write({'state': 'done', 'import_summary': summary})

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'pricelist.import.wizard',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_reset(self):
        self.write({'state': 'draft', 'import_summary': False, 'import_file': False,
                    'import_file_name': False})
        self.preview_line_ids.unlink()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'pricelist.import.wizard',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_close(self):
        return {'type': 'ir.actions.act_window_close'}


class PricelistImportWizardLine(models.TransientModel):
    _name = 'pricelist.import.wizard.line'
    _description = 'Pricelist Import Wizard – Preview Line'

    wizard_id = fields.Many2one('pricelist.import.wizard', ondelete='cascade')
    item_code = fields.Char(string='Item Code', readonly=True)
    product_tmpl_id = fields.Many2one('product.template', string='Product', readonly=True)
    product_name = fields.Char(string='Product Name', readonly=True)
    fixed_price = fields.Float(string='Fixed Price', digits='Product Price', readonly=True)
    status = fields.Selection(
        [('ok', 'Ready'), ('error', 'Error')],
        string='Status',
        readonly=True,
    )
    error_message = fields.Char(string='Error', readonly=True)

import base64
import io
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

try:
    import xlrd
except ImportError:
    xlrd = None

try:
    import openpyxl
except ImportError:
    openpyxl = None


# Magic bytes used to recognise the real file format regardless of its name
ZIP_MAGIC = b'PK\x03\x04'                          # .xlsx / .xlsm (zip)
OLE2_MAGIC = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'   # legacy .xls (OLE2)

# Encodings tried in order when reading a CSV
CSV_ENCODINGS = ('utf-8-sig', 'cp1256', 'cp1252')

# Formats that get uploaded by mistake. Recognised so the error can name them
# instead of failing later with a confusing "column not found".
FOREIGN_MAGIC = (
    (b'%PDF', 'PDF document'),
    (b'\x89PNG\r\n\x1a\n', 'PNG image'),
    (b'\xff\xd8\xff', 'JPEG image'),
    (b'GIF8', 'GIF image'),
    (b'BM', 'BMP image'),
    (b'{\\rtf', 'RTF document'),
    (b'<?xml', 'XML file (Excel 2003 XML is not supported)'),
    (b'\x1f\x8b', 'gzip archive'),
    (b'Rar!', 'RAR archive'),
    (b'7z\xbc\xaf\x27\x1c', '7-Zip archive'),
)

# Members that identify what a zip-based Office file really is
ZIP_SIGNATURES = (
    ('xl/', None),                              # .xlsx / .xlsm -> readable
    ('word/', 'Word document'),
    ('ppt/', 'PowerPoint presentation'),
)


# ──────────────────────────────────────────────────────────────────────────────
# Column aliases — headers are normalised to lower_snake_case before matching
# ──────────────────────────────────────────────────────────────────────────────
COLUMN_ALIASES = {
    # target columns
    'item_code': ('item_code', 'internal_reference', 'default_code', 'product_code',
                  'code', 'ref', 'reference', 'barcode', 'sku', 'product'),
    'category': ('category', 'categ', 'categ_id', 'product_category',
                 'category_name', 'categ_name'),
    # shared
    'min_quantity': ('min_quantity', 'min_qty', 'minimum_quantity', 'qty', 'quantity'),
    # compute_price = fixed
    'fixed_price': ('fixed_price', 'price', 'unit_price', 'sales_price',
                    'list_price', 'sale_price', 'new_price'),
    # compute_price = percentage
    'percent_price': ('percent_price', 'percentage', 'discount', 'percent',
                      'discount_percent', 'percentage_price'),
    # compute_price = formula
    'price_discount': ('price_discount', 'formula_discount', 'discount_formula',
                       'discount', 'percentage'),
    'price_surcharge': ('price_surcharge', 'surcharge', 'extra_fee', 'extra'),
    'price_round': ('price_round', 'rounding', 'round', 'rounding_method'),
    'price_min_margin': ('price_min_margin', 'min_margin', 'minimum_margin'),
    'price_max_margin': ('price_max_margin', 'max_margin', 'maximum_margin'),
}

# Target column each applied_on option needs in the sheet
TARGET_COLUMN = {
    '3_global': None,
    '2_product_category': 'category',
    '1_product': 'item_code',
    '0_product_variant': 'item_code',
}

# Value columns per compute_price: (at_least_one_of, optional)
VALUE_COLUMNS = {
    'fixed': (('fixed_price',), ()),
    'percentage': (('percent_price',), ()),
    'formula': (
        ('price_discount', 'price_surcharge', 'price_round',
         'price_min_margin', 'price_max_margin'),
        (),
    ),
}

# Every numeric value field on the item model, so unused ones get zeroed
ALL_VALUE_FIELDS = ('fixed_price', 'percent_price', 'price_discount',
                    'price_surcharge', 'price_round',
                    'price_min_margin', 'price_max_margin')


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
        help='Upload an Excel (.xlsx / .xls) or CSV file. Which columns the '
             'sheet must contain depends on "Apply On" and "Compute Price".',
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
        help='Level the imported rules apply to. The sheet must carry the '
             'matching target column:\n'
             '  - All Products: no target column needed\n'
             '  - Product Category: "category" (name or full path)\n'
             '  - Product / Product Variant: "item_code" '
             '(barcode or internal reference)',
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
        help='How each rule computes its price. The sheet must carry the '
             'matching value column(s):\n'
             '  - Fixed Price: "fixed_price"\n'
             '  - Discount: "percent_price"\n'
             '  - Formula: any of "price_discount", "price_surcharge", '
             '"price_round", "price_min_margin", "price_max_margin"',
    )
    formula_base = fields.Selection(
        selection=[
            ('list_price', 'Sales Price'),
            ('standard_price', 'Cost'),
            ('pricelist', 'Other Pricelist'),
        ],
        string='Based on',
        default='list_price',
        help='Base price the Formula rules are computed from.',
    )
    base_pricelist_id = fields.Many2one(
        'product.pricelist',
        string='Other Pricelist',
        help='Pricelist used as the base when "Based on" is "Other Pricelist".',
    )
    date_start = fields.Date(string='Start Date', required=True)
    date_end = fields.Date(string='End Date', required=True)
    overwrite_existing = fields.Boolean(
        string='Overwrite Existing Items',
        default=True,
        help='If checked, an existing item with the same target '
             '(product / variant / category / global) and minimum quantity is '
             'updated instead of creating a duplicate.',
    )
    expected_columns = fields.Text(
        string='Expected Columns',
        compute='_compute_expected_columns',
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
    # Defaults / computes / onchanges
    # ──────────────────────────────────────────────────────────────────────

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        active_id = self.env.context.get('active_id')
        if active_id:
            res['pricelist_id'] = active_id
        return res

    @api.depends('applied_on', 'compute_price')
    def _compute_expected_columns(self):
        applied_labels = dict(self._fields['applied_on'].selection)
        for wizard in self:
            parts = []
            target = TARGET_COLUMN.get(wizard.applied_on)
            if target == 'item_code':
                label = (_('product variant')
                         if wizard.applied_on == '0_product_variant'
                         else _('product'))
                parts.append(_('item_code - barcode or internal reference of the %s')
                             % label)
            elif target == 'category':
                parts.append(_('category - category name or full path '
                               '(e.g. "All / Saleable / Phones")'))
            else:
                parts.append(_('no target column - every row applies to all products'))

            required = VALUE_COLUMNS.get(wizard.compute_price, ((), ()))[0]
            if wizard.compute_price == 'formula':
                parts.append(_('at least one of: %s') % ', '.join(required))
            elif required:
                parts.append(_('%s - required') % required[0])
            parts.append(_('min_quantity - optional, defaults to 0'))
            wizard.expected_columns = _('Apply On: %s') % applied_labels.get(
                wizard.applied_on, '') + '\n' + '\n'.join('- %s' % p for p in parts)

    @api.onchange('compute_price')
    def _onchange_compute_price(self):
        if self.compute_price != 'formula':
            self.formula_base = 'list_price'
            self.base_pricelist_id = False

    @api.onchange('formula_base')
    def _onchange_formula_base(self):
        if self.formula_base != 'pricelist':
            self.base_pricelist_id = False

    # ──────────────────────────────────────────────────────────────────────
    # File parsing
    # ──────────────────────────────────────────────────────────────────────

    def _detect_format(self, file_name, file_bytes):
        """Return 'xlsx', 'xls' or 'csv'.

        Decided on the file's own content, not its name: users rename
        spreadsheets, export .xls from systems that really write CSV, and
        browsers drop extensions. The two workbook formats have unambiguous
        magic bytes, so anything else must be readable as text to be usable.
        """
        label = file_name or _('The uploaded file')

        for magic, description in FOREIGN_MAGIC:
            if file_bytes.startswith(magic):
                raise UserError(
                    _('"%(name)s" is a %(kind)s, not a spreadsheet.\n'
                      'Please upload a .xlsx, .xls or .csv file.')
                    % {'name': label, 'kind': description}
                )

        if file_bytes.startswith(ZIP_MAGIC):
            return self._detect_zip_format(file_bytes, label)
        if file_bytes.startswith(OLE2_MAGIC):
            return 'xls'           # legacy .xls — OLE2 compound file
        if self._looks_like_text(file_bytes):
            return 'csv'
        raise UserError(
            _('"%(name)s" is not a spreadsheet Odoo can read.\n'
              'Please upload a .xlsx, .xls or .csv file.')
            % {'name': label}
        )

    def _detect_zip_format(self, file_bytes, label):
        """An Office Open XML file is a zip — look inside to see which one."""
        import zipfile
        try:
            names = zipfile.ZipFile(io.BytesIO(file_bytes)).namelist()
        except Exception:
            # Broken archive: let the xlsx parser raise the corrupt-file error
            return 'xlsx'

        for prefix, description in ZIP_SIGNATURES:
            if any(name.startswith(prefix) for name in names):
                if description is None:
                    return 'xlsx'
                raise UserError(
                    _('"%(name)s" is a %(kind)s, not a spreadsheet.\n'
                      'Please upload a .xlsx, .xls or .csv file.')
                    % {'name': label, 'kind': description}
                )

        if any(name == 'content.xml' for name in names):
            raise UserError(
                _('"%(name)s" is an OpenDocument file (.ods / .odt), which is '
                  'not supported.\nPlease re-save it as .xlsx or .csv.')
                % {'name': label}
            )
        return 'xlsx'

    @staticmethod
    def _looks_like_text(file_bytes):
        """True when the bytes decode to mostly printable text (so, a CSV)."""
        sample = file_bytes[:4096]
        if not sample or b'\x00' in sample:
            return False
        for encoding in ('utf-8-sig', 'cp1256', 'cp1252'):
            try:
                text = sample.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            return False
        if not text.strip():
            return False
        printable = sum(1 for char in text if char.isprintable() or char in '\r\n\t')
        return printable / len(text) > 0.9

    def _parse_file(self):
        """Parse the uploaded file into a list of row dicts."""
        self.ensure_one()
        if not self.import_file:
            raise UserError(_('Please upload a file before continuing.'))

        file_name = (self.import_file_name or '').strip()
        file_bytes = base64.b64decode(self.import_file)

        file_format = self._detect_format(file_name, file_bytes)
        parser = {
            'xlsx': self._parse_xlsx,
            'xls': self._parse_xls,
            'csv': self._parse_csv,
        }[file_format]

        try:
            rows = parser(file_bytes)
        except UserError:
            raise
        except Exception as exc:
            _logger.exception('Pricelist import: failed to read %s as %s',
                              file_name, file_format)
            raise UserError(
                _('"%(name)s" looks like a %(fmt)s file but could not be read. '
                  'It may be corrupted or password-protected.\n\n%(err)s')
                % {'name': file_name or _('The uploaded file'),
                   'fmt': file_format, 'err': exc}
            ) from exc
        return self._rows_to_dicts(rows)

    def _normalize_header(self, raw):
        return (raw or '').strip().lower().replace(' ', '_').replace('-', '_')

    def _parse_xlsx(self, file_bytes):
        if not openpyxl:
            raise UserError(
                _('Python library "openpyxl" is required to read .xlsx files. '
                  'Ask your system administrator to install it.')
            )
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True,
                                    data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            raise UserError(_('The file is empty.'))
        return rows

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
        return rows

    def _parse_csv(self, file_bytes):
        import csv as csv_mod
        for encoding in CSV_ENCODINGS:
            try:
                text = file_bytes.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            # Last resort: never fail on one bad byte, but log that we lost data
            text = file_bytes.decode('utf-8', errors='replace')
            _logger.warning('Pricelist import: CSV is not valid %s, '
                            'some characters were replaced.',
                            ' / '.join(CSV_ENCODINGS))
        try:
            dialect = csv_mod.Sniffer().sniff(text[:4096], delimiters=',;\t|')
        except csv_mod.Error:
            dialect = csv_mod.excel
        reader = csv_mod.reader(io.StringIO(text), dialect)
        # Blank rows are kept so reported row numbers match the real sheet lines;
        # _rows_to_dicts skips them.
        rows = [tuple(row) for row in reader]
        while rows and not any((c or '').strip() for c in rows[-1]):
            rows.pop()
        if not rows:
            raise UserError(_('The file is empty.'))
        return rows

    # ── column resolution ─────────────────────────────────────────────────

    def _resolve_columns(self, headers):
        """Map canonical field name -> column index for the current mode."""
        self.ensure_one()
        target = TARGET_COLUMN.get(self.applied_on)
        required, optional = VALUE_COLUMNS.get(self.compute_price, ((), ()))

        wanted = []
        if target:
            wanted.append(target)
        wanted += list(required) + list(optional) + ['min_quantity']

        mapping = {}
        for field in wanted:
            for alias in COLUMN_ALIASES.get(field, ()):
                if alias in headers:
                    idx = headers.index(alias)
                    if idx not in mapping.values():
                        mapping[field] = idx
                        break

        if target and target not in mapping:
            raise UserError(
                _('Apply On is "%(level)s", so the sheet needs the "%(col)s" column.\n'
                  'Accepted names: %(aliases)s\n'
                  'Detected headers: %(headers)s') % {
                    'level': dict(self._fields['applied_on'].selection)[self.applied_on],
                    'col': target,
                    'aliases': ', '.join(COLUMN_ALIASES[target]),
                    'headers': ', '.join(h for h in headers if h) or _('(none)'),
                }
            )

        if required and not any(f in mapping for f in required):
            mode_label = dict(self._fields['compute_price'].selection)[self.compute_price]
            need = (_('at least one of %s') % ', '.join(required)
                    if len(required) > 1 else _('the "%s" column') % required[0])
            raise UserError(
                _('Compute Price is "%(mode)s", so the sheet needs %(need)s.\n'
                  'Accepted names: %(aliases)s\n'
                  'Detected headers: %(headers)s') % {
                    'mode': mode_label,
                    'need': need,
                    'aliases': ', '.join(
                        dict.fromkeys(
                            a for f in required for a in COLUMN_ALIASES.get(f, ())
                        )
                    ),
                    'headers': ', '.join(h for h in headers if h) or _('(none)'),
                }
            )
        return mapping

    @staticmethod
    def _to_float(value):
        """Return a float, or None for a blank cell. Raises ValueError on garbage."""
        if value is None:
            return None
        if isinstance(value, bool):
            return float(value)
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip().replace('%', '').replace(' ', '')
        if not text:
            return None
        if ',' in text and '.' in text:
            # 1.234,56 -> 1234.56   /   1,234.56 -> 1234.56
            if text.rindex(',') > text.rindex('.'):
                text = text.replace('.', '').replace(',', '.')
            else:
                text = text.replace(',', '')
        else:
            text = text.replace(',', '.')
        return float(text)

    def _rows_to_dicts(self, rows):
        headers = [self._normalize_header(str(h) if h is not None else '')
                   for h in rows[0]]
        mapping = self._resolve_columns(headers)
        required = VALUE_COLUMNS.get(self.compute_price, ((), ()))[0]
        target = TARGET_COLUMN.get(self.applied_on)

        data = []
        for row_idx, row in enumerate(rows[1:], start=2):
            if not any(str(c).strip() for c in row if c is not None):
                continue
            record = {'row_number': row_idx}

            if target:
                idx = mapping[target]
                raw = row[idx] if idx < len(row) else None
                code = '' if raw is None else str(raw).strip()
                # Excel often turns a numeric barcode into 1234567890.0
                if code.endswith('.0') and code[:-2].isdigit():
                    code = code[:-2]
                if not code:
                    continue
                record['target_code'] = code

            for field in list(required) + ['min_quantity']:
                if field not in mapping:
                    continue
                idx = mapping[field]
                raw = row[idx] if idx < len(row) else None
                try:
                    record[field] = self._to_float(raw)
                except (ValueError, TypeError):
                    raise UserError(
                        _('Row %(row)d: invalid numeric value "%(val)s" in '
                          'column "%(col)s".') % {
                            'row': row_idx, 'val': raw, 'col': field,
                        }
                    )
            data.append(record)

        if not data:
            raise UserError(_('No valid data rows found in the file.'))
        return data

    # ──────────────────────────────────────────────────────────────────────
    # Target resolution
    # ──────────────────────────────────────────────────────────────────────

    def _resolve_target(self, code):
        """Return (vals_fragment, display_name, status, message) for one row."""
        self.ensure_one()
        applied_on = self.applied_on

        if applied_on == '3_global':
            return {}, _('All Products'), 'ok', ''

        if applied_on == '2_product_category':
            Category = self.env['product.category']
            categ = Category.search([('complete_name', '=', code)], limit=2)
            if not categ:
                categ = Category.search([('name', '=', code)], limit=2)
            if not categ:
                categ = Category.search([('complete_name', 'ilike', code)], limit=2)
            if not categ:
                return {}, _('-- Not Found --'), 'error', _('Product category not found')
            if len(categ) > 1:
                return ({'categ_id': categ[0].id}, categ[0].complete_name, 'warning',
                        _('Several categories match - using "%s"')
                        % categ[0].complete_name)
            return {'categ_id': categ.id}, categ.complete_name, 'ok', ''

        if applied_on == '0_product_variant':
            Product = self.env['product.product']
            products = Product.search([('barcode', '=', code)], limit=2)
            if not products:
                products = Product.search([('default_code', '=', code)], limit=2)
            if not products:
                return {}, _('-- Not Found --'), 'error', _('Product variant not found')
            product = products[0]
            frag = {'product_id': product.id,
                    'product_tmpl_id': product.product_tmpl_id.id}
            if len(products) > 1:
                return (frag, product.display_name, 'warning',
                        _('Several variants match - using "%s"') % product.display_name)
            return frag, product.display_name, 'ok', ''

        # '1_product'
        Template = self.env['product.template']
        templates = Template.search([('barcode', '=', code)], limit=2)
        if not templates:
            templates = Template.search([('default_code', '=', code)], limit=2)
        if not templates:
            variants = self.env['product.product'].search(
                ['|', ('barcode', '=', code), ('default_code', '=', code)], limit=2
            )
            templates = variants.product_tmpl_id
        if not templates:
            return {}, _('-- Not Found --'), 'error', _('Product not found')
        template = templates[0]
        if len(templates) > 1:
            return ({'product_tmpl_id': template.id}, template.display_name, 'warning',
                    _('Several products match - using "%s"') % template.display_name)
        return {'product_tmpl_id': template.id}, template.display_name, 'ok', ''

    # ──────────────────────────────────────────────────────────────────────
    # Actions
    # ──────────────────────────────────────────────────────────────────────

    def _check_options(self):
        self.ensure_one()
        if self.date_start and self.date_end and self.date_end < self.date_start:
            raise UserError(_('End Date cannot be earlier than Start Date.'))
        if self.compute_price == 'formula' and self.formula_base == 'pricelist':
            if not self.base_pricelist_id:
                raise UserError(
                    _('"Based on" is "Other Pricelist", so please pick the '
                      'pricelist to compute from.')
                )
            if self.base_pricelist_id == self.pricelist_id:
                raise UserError(
                    _('The base pricelist cannot be the pricelist being '
                      'imported into.')
                )

    def action_preview(self):
        """Parse the file and build the preview lines."""
        self.ensure_one()
        self._check_options()
        rows = self._parse_file()
        required = VALUE_COLUMNS.get(self.compute_price, ((), ()))[0]
        has_target = bool(TARGET_COLUMN.get(self.applied_on))

        preview_vals = []
        for row in rows:
            if has_target:
                frag, display, status, message = self._resolve_target(
                    row['target_code'])
            else:
                frag, display, status, message = {}, _('All Products'), 'ok', ''

            vals = {
                'wizard_id': self.id,
                'row_number': row['row_number'],
                'item_code': row.get('target_code') or _('All Products'),
                'product_tmpl_id': frag.get('product_tmpl_id', False),
                'product_id': frag.get('product_id', False),
                'categ_id': frag.get('categ_id', False),
                'product_name': display,
                'min_quantity': row.get('min_quantity') or 0.0,
                'status': status,
                'error_message': message,
            }
            for field in ALL_VALUE_FIELDS:
                if row.get(field) is not None:
                    vals[field] = row[field]

            if not any(row.get(f) is not None for f in required):
                vals['status'] = 'error'
                vals['error_message'] = _('No value found in %s') % ', '.join(required)
            preview_vals.append(vals)

        self.preview_line_ids.unlink()
        self.env['pricelist.import.wizard.line'].create(preview_vals)

        ok = sum(1 for v in preview_vals if v['status'] == 'ok')
        warn = sum(1 for v in preview_vals if v['status'] == 'warning')
        err = sum(1 for v in preview_vals if v['status'] == 'error')
        summary = _('%(ok)d ready, %(warn)d with warnings, %(err)d in error '
                    '(error rows are skipped).') % {'ok': ok, 'warn': warn, 'err': err}
        self.write({'state': 'preview', 'import_summary': summary})
        return self._reopen()

    def action_import(self):
        """Create / update pricelist items for every importable preview line."""
        self.ensure_one()
        self._check_options()
        lines = self.preview_line_ids.filtered(lambda l: l.status in ('ok', 'warning'))
        if not lines:
            raise UserError(
                _('No importable rows. Please fix the errors shown in the '
                  'preview and try again.')
            )

        PricelistItem = self.env['product.pricelist.item']
        created = updated = skipped = 0

        for line in lines:
            vals = self._prepare_item_vals(line)
            existing = PricelistItem.search(self._existing_domain(line), limit=1)
            if existing:
                if self.overwrite_existing:
                    existing.write(vals)
                    updated += 1
                else:
                    skipped += 1
            else:
                PricelistItem.create(vals)
                created += 1

        summary = _('%(c)d created, %(u)d updated, %(s)d skipped '
                    '(already existing).') % {'c': created, 'u': updated,
                                              's': skipped}
        self.write({'state': 'done', 'import_summary': summary})
        return self._reopen()

    def _prepare_item_vals(self, line):
        """Build product.pricelist.item values honouring applied_on / compute_price."""
        self.ensure_one()
        vals = {
            'pricelist_id': self.pricelist_id.id,
            'applied_on': self.applied_on,
            'compute_price': self.compute_price,
            'min_quantity': line.min_quantity or 0.0,
            'date_start': self.date_start or False,
            'date_end': self.date_end or False,
            # clear every target, then set only the one this level uses
            'product_tmpl_id': False,
            'product_id': False,
            'categ_id': False,
        }
        if self.applied_on == '1_product':
            vals['product_tmpl_id'] = line.product_tmpl_id.id
        elif self.applied_on == '0_product_variant':
            vals['product_id'] = line.product_id.id
            vals['product_tmpl_id'] = line.product_tmpl_id.id
        elif self.applied_on == '2_product_category':
            vals['categ_id'] = line.categ_id.id

        # zero every value field, then fill only the ones this mode uses
        for field in ALL_VALUE_FIELDS:
            vals[field] = 0.0

        if self.compute_price == 'fixed':
            vals['fixed_price'] = line.fixed_price or 0.0
            vals['base'] = 'list_price'
            vals['base_pricelist_id'] = False
        elif self.compute_price == 'percentage':
            vals['percent_price'] = line.percent_price or 0.0
            vals['base'] = 'list_price'
            vals['base_pricelist_id'] = False
        else:  # formula
            vals['price_discount'] = line.price_discount or 0.0
            vals['price_surcharge'] = line.price_surcharge or 0.0
            vals['price_round'] = line.price_round or 0.0
            vals['price_min_margin'] = line.price_min_margin or 0.0
            vals['price_max_margin'] = line.price_max_margin or 0.0
            vals['base'] = self.formula_base
            vals['base_pricelist_id'] = (
                self.base_pricelist_id.id
                if self.formula_base == 'pricelist' else False
            )
        return vals

    def _existing_domain(self, line):
        """Domain identifying an already-existing rule matching this line."""
        self.ensure_one()
        domain = [
            ('pricelist_id', '=', self.pricelist_id.id),
            ('applied_on', '=', self.applied_on),
            ('min_quantity', '=', line.min_quantity or 0.0),
        ]
        if self.applied_on == '1_product':
            domain.append(('product_tmpl_id', '=', line.product_tmpl_id.id))
        elif self.applied_on == '0_product_variant':
            domain.append(('product_id', '=', line.product_id.id))
        elif self.applied_on == '2_product_category':
            domain.append(('categ_id', '=', line.categ_id.id))
        return domain

    def _reopen(self):
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'pricelist.import.wizard',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_reset(self):
        self.write({'state': 'draft', 'import_summary': False,
                    'import_file': False, 'import_file_name': False})
        self.preview_line_ids.unlink()
        return self._reopen()

    def action_close(self):
        return {'type': 'ir.actions.act_window_close'}


class PricelistImportWizardLine(models.TransientModel):
    _name = 'pricelist.import.wizard.line'
    _description = 'Pricelist Import Wizard - Preview Line'
    _order = 'row_number'

    wizard_id = fields.Many2one('pricelist.import.wizard', ondelete='cascade')
    row_number = fields.Integer(string='Row', readonly=True)
    item_code = fields.Char(string='Target Code', readonly=True)
    product_tmpl_id = fields.Many2one('product.template', string='Product',
                                      readonly=True)
    product_id = fields.Many2one('product.product', string='Variant', readonly=True)
    categ_id = fields.Many2one('product.category', string='Category', readonly=True)
    product_name = fields.Char(string='Resolved Target', readonly=True)
    min_quantity = fields.Float(string='Min. Quantity', readonly=True)

    fixed_price = fields.Float(string='Fixed Price', digits='Product Price',
                               readonly=True)
    percent_price = fields.Float(string='Discount (%)', readonly=True)
    price_discount = fields.Float(string='Formula Discount (%)', readonly=True)
    price_surcharge = fields.Float(string='Extra Fee', digits='Product Price',
                                   readonly=True)
    price_round = fields.Float(string='Rounding', digits='Product Price',
                               readonly=True)
    price_min_margin = fields.Float(string='Min. Margin', digits='Product Price',
                                    readonly=True)
    price_max_margin = fields.Float(string='Max. Margin', digits='Product Price',
                                    readonly=True)

    status = fields.Selection(
        [('ok', 'Ready'), ('warning', 'Warning'), ('error', 'Error')],
        string='Status',
        readonly=True,
    )
    error_message = fields.Char(string='Message', readonly=True)

import base64
import io

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class StockMultiUpdateImportWizard(models.TransientModel):
    _name = 'stock.multi.update.import.wizard'
    _description = 'Import Lines from Excel'

    update_id = fields.Many2one(
        'stock.multi.update',
        string='Update Order',
        required=True,
        ondelete='cascade',
    )
    excel_file = fields.Binary(
        string='Excel File',
        required=True,
        attachment=False,
    )
    excel_filename = fields.Char(string='Filename')

    # Result feedback
    state = fields.Selection([
        ('upload', 'Upload'),
        ('preview', 'Preview'),
    ], default='upload')

    preview_line_ids = fields.One2many(
        'stock.multi.update.import.preview.line',
        'wizard_id',
        string='Preview Lines',
        readonly=True,
    )
    error_count = fields.Integer(compute='_compute_counts')
    success_count = fields.Integer(compute='_compute_counts')

    @api.depends('preview_line_ids.status')
    def _compute_counts(self):
        for rec in self:
            rec.error_count = len(rec.preview_line_ids.filtered(lambda l: l.status == 'error'))
            rec.success_count = len(rec.preview_line_ids.filtered(lambda l: l.status == 'ok'))

    def action_parse(self):
        """Parse the Excel file and show preview."""
        self.ensure_one()
        if not self.excel_file:
            raise UserError(_('Please upload an Excel file.'))

        try:
            import openpyxl
        except ImportError:
            raise UserError(_('openpyxl library is required. Install it via: pip install openpyxl'))

        file_data = base64.b64decode(self.excel_file)
        try:
            wb = openpyxl.load_workbook(io.BytesIO(file_data), data_only=True)
        except Exception as e:
            raise UserError(_('Cannot read Excel file: %s') % str(e))

        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))

        if len(rows) < 2:
            raise UserError(_('The Excel file must have a header row and at least one data row.'))

        # Detect header row (first row)
        header = [str(c).strip().lower() if c else '' for c in rows[0]]

        # Map expected columns (flexible matching)
        col_map = self._map_columns(header)
        missing = [k for k, v in col_map.items() if v is None and k in ('product', 'qty', 'operation')]
        if missing:
            raise UserError(
                _('Missing required column(s): %s\n\nFound columns: %s')
                % (', '.join(missing), ', '.join(c for c in header if c))
            )

        # Delete old preview lines
        self.preview_line_ids.unlink()

        preview_vals = []
        for row_num, row in enumerate(rows[1:], start=2):
            if all(v is None for v in row):
                continue  # skip empty rows

            def get(key):
                idx = col_map.get(key)
                if idx is None:
                    return None
                val = row[idx]
                return str(val).strip() if val is not None else None

            product_str = get('product')
            qty_raw = get('qty')
            operation_raw = get('operation')
            lot_raw = get('lot')

            status = 'ok'
            error_msg = ''
            product_id = False
            operation = 'add'

            # ── Validate product ──────────────────────────────────────────
            if not product_str:
                status = 'error'
                error_msg = _('Row %d: Product is empty.') % row_num
            else:
                product = (
                    self.env['product.product'].search(
                        [('barcode', '=', product_str)], limit=1)
                    or self.env['product.product'].search(
                        [('default_code', '=', product_str)], limit=1)
                    or self.env['product.product'].search(
                        [('name', 'ilike', product_str), ('type', '=', 'consu')], limit=1)
                )
                if not product:
                    status = 'error'
                    error_msg = _('Row %d: Product "%s" not found.') % (row_num, product_str)
                else:
                    product_id = product.id

            # ── Validate qty ──────────────────────────────────────────────
            try:
                qty = float(qty_raw) if qty_raw else 0.0
                if qty <= 0:
                    status = 'error'
                    error_msg = (error_msg + ' ' if error_msg else '') + \
                        _('Row %d: Quantity must be > 0.') % row_num
            except (ValueError, TypeError):
                qty = 0.0
                status = 'error'
                error_msg = (error_msg + ' ' if error_msg else '') + \
                    _('Row %d: Invalid quantity "%s".') % (row_num, qty_raw)

            # ── Validate operation ────────────────────────────────────────
            if operation_raw:
                op_lower = operation_raw.lower()
                if op_lower in ('add', '+', 'in', 'receive', 'plus'):
                    operation = 'add'
                elif op_lower in ('subtract', '-', 'out', 'remove', 'minus'):
                    operation = 'subtract'
                else:
                    status = 'error'
                    error_msg = (error_msg + ' ' if error_msg else '') + \
                        _('Row %d: Unknown operation "%s". Use add/subtract.') % (row_num, operation_raw)
            # default is 'add' if column missing

            preview_vals.append({
                'wizard_id': self.id,
                'row_num': row_num,
                'product_id': product_id,
                'product_raw': product_str or '',
                'lot_serial': lot_raw or '',
                'operation': operation,
                'qty': qty,
                'status': status,
                'error_msg': error_msg,
            })

        if not preview_vals:
            raise UserError(_('No data rows found in the Excel file.'))

        self.env['stock.multi.update.import.preview.line'].create(preview_vals)
        self.state = 'preview'

        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_import(self):
        """Import only valid preview lines as update lines."""
        self.ensure_one()
        valid_lines = self.preview_line_ids.filtered(lambda l: l.status == 'ok')
        if not valid_lines:
            raise UserError(_('No valid lines to import. Please fix errors in the Excel file and re-upload.'))

        line_vals = []
        for pl in valid_lines:
            lot_id = False
            lot_name = False
            if pl.lot_serial:
                lot = self.env['stock.lot'].search([
                    ('name', '=', pl.lot_serial),
                    ('product_id', '=', pl.product_id.id),
                ], limit=1)
                if lot:
                    lot_id = lot.id
                else:
                    lot_name = pl.lot_serial  # will be created on apply

            line_vals.append({
                'update_id': self.update_id.id,
                'product_id': pl.product_id.id,
                'lot_id': lot_id,
                'lot_name': lot_name,
                'operation': pl.operation,
                'qty': pl.qty,
            })

        self.env['stock.multi.update.line'].create(line_vals)

        return {
            'type': 'ir.actions.client',
            "tag": "reload",

        }

    def action_download_template(self):
        """Return a base64-encoded Excel template as a download."""
        return {
            'type': 'ir.actions.act_url',
            'url': '/stock_multi_update/download_template',
            'target': 'self',
        }

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _map_columns(self, header):
        """Map logical column names to their 0-based index in header list."""
        mapping = {
            'product':   ['product', 'product name', 'item', 'barcode', 'internal ref', 'ref', 'product_id', 'sku'],
            'qty':       ['qty', 'quantity', 'amount', 'count'],
            'operation': ['operation', 'op', 'action', 'type', 'direction'],
            'lot':       ['lot', 'serial', 'lot/serial', 'lot number', 'serial number', 'lot_id', 'serial no', 'lot no'],
        }
        result = {k: None for k in mapping}
        for logical, aliases in mapping.items():
            for alias in aliases:
                if alias in header:
                    result[logical] = header.index(alias)
                    break
        return result


class StockMultiUpdateImportPreviewLine(models.TransientModel):
    _name = 'stock.multi.update.import.preview.line'
    _description = 'Import Preview Line'
    _order = 'row_num'

    wizard_id = fields.Many2one('stock.multi.update.import.wizard', ondelete='cascade')
    row_num = fields.Integer(string='Row #', readonly=True)
    product_id = fields.Many2one('product.product', string='Product', readonly=True)
    product_raw = fields.Char(string='Raw Product', readonly=True)
    lot_serial = fields.Char(string='Lot / Serial', readonly=True)
    operation = fields.Selection([
        ('add', 'Add ➕'),
        ('subtract', 'Subtract ➖'),
    ], readonly=True)
    qty = fields.Float(string='Qty', digits='Product Unit of Measure', readonly=True)
    note = fields.Char(string='Note', readonly=True)
    status = fields.Selection([
        ('ok', 'OK ✅'),
        ('error', 'Error ❌'),
    ], readonly=True)
    error_msg = fields.Char(string='Error', readonly=True)

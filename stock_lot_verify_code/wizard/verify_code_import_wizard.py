import base64
from io import BytesIO

from odoo import fields, models, _
from odoo.exceptions import UserError, ValidationError

try:
    import openpyxl
except ImportError:
    openpyxl = None


class StockLotVerifyCodeImportWizard(models.TransientModel):
    _name = 'stock.lot.verify.code.import.wizard'
    _description = 'Import Lot Verify Codes from Excel'

    file = fields.Binary(string='Excel File', required=True)
    filename = fields.Char(string='File Name')

    def action_import(self):
        self.ensure_one()
        if openpyxl is None:
            raise UserError(_('The Python library "openpyxl" is required to read Excel files.'))
        if not self.file:
            raise UserError(_('Please upload an Excel (.xlsx) file.'))
        try:
            data = base64.b64decode(self.file)
            workbook = openpyxl.load_workbook(filename=BytesIO(data), data_only=True, read_only=True)
        except Exception as exc:
            raise UserError(_('The uploaded file is not a valid Excel (.xlsx) file.')) from exc

        sheet = workbook.active
        Lot = self.env['stock.lot']
        updated_lots = 0
        not_found = []
        invalid_rows = []
        conflicts = []
        seen_codes = {}

        # Row 1 is treated as a header (serial, code); data starts at row 2.
        for row_no, cells in enumerate(sheet.iter_rows(min_row=2, max_col=2, values_only=True), start=2):
            serial = cells[0] if len(cells) > 0 else None
            code = cells[1] if len(cells) > 1 else None

            if (serial is None or str(serial).strip() == '') and code is None:
                continue  # fully empty row

            serial = str(serial).strip() if serial is not None else ''
            if not serial:
                invalid_rows.append(_('Row %s: missing serial.') % row_no)
                continue

            # Verify codes are identifiers, not numbers, so keep them as text.
            # Excel hands whole numbers back as floats (e.g. 123456789012.0),
            # so drop the trailing ".0" for those to get a clean digit string.
            if isinstance(code, float) and code.is_integer():
                verify_code = str(int(code))
            elif code is None:
                verify_code = ''
            else:
                verify_code = str(code).strip()
            if not verify_code:
                invalid_rows.append(
                    _('Row %(row)s (serial %(serial)s): invalid code "%(code)s".')
                    % {'row': row_no, 'serial': serial, 'code': code})
                continue

            # Duplicate code inside the same file (on a different serial).
            if verify_code in seen_codes and seen_codes[verify_code] != serial:
                conflicts.append(
                    _('Row %(row)s: code %(code)s already used in this file by serial %(other)s.')
                    % {'row': row_no, 'code': verify_code, 'other': seen_codes[verify_code]})
                continue

            lots = Lot.search([('name', '=', serial)])
            if not lots:
                not_found.append(serial)
                continue

            # Isolate each write so a uniqueness clash reports the row instead
            # of rolling back the whole import.
            try:
                with self.env.cr.savepoint():
                    lots.write({'verify_code': verify_code})
                    # Force the uniqueness @api.constrains to run inside the
                    # savepoint so a clash is caught here, per row.
                    self.env.flush_all()
            except ValidationError as exc:
                conflicts.append(
                    _('Row %(row)s (serial %(serial)s, code %(code)s): %(err)s')
                    % {'row': row_no, 'serial': serial, 'code': verify_code,
                       'err': ', '.join(exc.args) if exc.args else _('verify code is not unique')})
                continue

            seen_codes[verify_code] = serial
            updated_lots += len(lots)

        return self._notify(updated_lots, not_found, invalid_rows, conflicts)

    def _notify(self, updated_lots, not_found, invalid_rows, conflicts):
        def _preview(items, limit=20):
            shown = items[:limit]
            text = ', '.join(items[:limit])
            if len(items) > limit:
                text += _(' … (+%s more)') % (len(items) - limit)
            return text

        message = _('Updated %s lot(s).') % updated_lots
        if not_found:
            message += '\n' + _('Serials not found (%(n)s): %(list)s') % {
                'n': len(not_found), 'list': _preview(not_found)}
        if conflicts:
            message += '\n' + _('Code conflicts (%(n)s):\n%(list)s') % {
                'n': len(conflicts), 'list': '\n'.join(conflicts[:20])}
        if invalid_rows:
            message += '\n' + _('Invalid rows (%(n)s):\n%(list)s') % {
                'n': len(invalid_rows), 'list': '\n'.join(invalid_rows[:20])}

        has_issue = bool(not_found or invalid_rows or conflicts)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Verify Code Import'),
                'message': message,
                'type': 'warning' if has_issue else 'success',
                'sticky': has_issue,
            },
        }

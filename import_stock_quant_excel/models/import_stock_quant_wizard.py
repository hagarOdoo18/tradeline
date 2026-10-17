import base64
import logging
import math
from collections import defaultdict
from io import BytesIO
from time import perf_counter

import openpyxl
from odoo import api, models, fields, _
from odoo.exceptions import UserError
from odoo.osv import expression
from odoo.tools import float_compare

_logger = logging.getLogger(__name__)

class ImportStockQuantWizard(models.Model):
    _name = 'import.stock.quant.wizard'
    _description = 'Import Stock Quant Wizard'
    _rec_name = 'filename'
    _order = 'id desc'

    # Native stock/accounting posting is deliberately kept intact. Large
    # workbooks run in committed cron batches rather than one HTTP request.
    _posting_batch_size = 100

    file = fields.Binary(required=True)
    filename = fields.Char()
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company, required=True)
    state = fields.Selection([
        ('draft', 'Draft'), ('preview', 'Preview'), ('processing', 'Processing'),
        ('failed', 'Needs Review'), ('done', 'Done'),
    ], default='draft', index=True, readonly=True)
    line_ids = fields.One2many('import.stock.quant.line','wizard_id')
    applied_rows = fields.Integer(string='Rows Processed', readonly=True)
    skipped_rows = fields.Integer(string='Rows Skipped', readonly=True)
    remaining_rows = fields.Integer(string='Rows Remaining', readonly=True)
    processing_error = fields.Text(readonly=True)

    def write(self, vals):
        if {'file', 'company_id'}.intersection(vals):
            if any(w.state not in ('draft', 'preview') for w in self):
                raise UserError(_('Started imports cannot be reused. Create a new import.'))
            vals = dict(vals, state='draft', applied_rows=0, skipped_rows=0, remaining_rows=0)
        return super().write(vals)

    def action_preview(self):
        self.ensure_one()
        if self.state not in ('draft', 'preview'):
            raise UserError(_('A started import cannot be previewed again.'))
        self._check_import_company()
        started = perf_counter()
        try:
            wb = openpyxl.load_workbook(BytesIO(base64.b64decode(self.file)), data_only=True, read_only=True)
            rows = []
            for row_no, cells in enumerate(wb.active.iter_rows(min_row=2, max_col=4, values_only=True), 2):
                if all(value is None or value == '' for value in cells):
                    continue
                location, item_code, raw_quantity, serial = cells
                error = False
                try:
                    quantity = float(raw_quantity or 0)
                    if not math.isfinite(quantity):
                        raise ValueError()
                except (ValueError, TypeError, OverflowError):
                    quantity = 0
                    error = _('Invalid quantity')
                rows.append({
                    'wizard_id': self.id, 'row_no': row_no,
                    'location_name': self._excel_identifier(location),
                    'item_code': self._excel_identifier(item_code),
                    'serial': self._excel_identifier(serial), 'quantity': quantity,
                    'is_valid': not error, 'error_msg': error,
                })
        except Exception as exc:
            raise UserError(_('Cannot read this Excel file. Upload a valid XLSX workbook.')) from exc
        finally:
            if 'wb' in locals():
                wb.close()
        if not rows:
            raise UserError(_('The workbook has no data rows.'))

        # Resolve each set once. A 5,000-row workbook must not do 5,000
        # product, location, lot and stock searches or individual line creates.
        Product = self.env['product.product'].with_company(self.company_id)
        Location = self.env['stock.location']
        products = Product.search([('barcode', 'in', list({r['item_code'] for r in rows if r['item_code']}))])
        locations = Location.search([
            ('complete_name', 'in', list({r['location_name'] for r in rows if r['location_name']})),
            ('usage', '=', 'internal'), ('company_id', 'in', [self.company_id.id, False]),
        ])
        products_by_code = {p.barcode: p for p in products}
        locations_by_name = {location.complete_name: location for location in locations}
        lots = self.env['stock.lot'].search([
            ('product_id', 'in', products.ids),
            ('name', 'in', list({r['serial'] for r in rows if r['serial']})),
            ('company_id', 'in', [self.company_id.id, False]),
        ])
        lots_by_key = {(lot.product_id.id, lot.name): lot for lot in lots.sorted(lambda lot: bool(lot.company_id))}
        quants = self.env['stock.quant'].with_company(self.company_id).search([
            ('product_id', 'in', products.ids), ('location_id', 'in', locations.ids),
            ('company_id', '=', self.company_id.id), ('lot_id', 'in', lots.ids + [False]),
        ])
        quantities = defaultdict(float)
        for quant in quants:
            quantities[(quant.product_id.id, quant.location_id.id, quant.lot_id.id)] += quant.quantity
        serial_positions = self._serial_positions(lots.filtered(lambda lot: lot.product_id.tracking == 'serial'))
        serial_signs = defaultdict(set)
        for vals in rows:
            if vals['serial'] and vals['quantity']:
                serial_signs[(vals['item_code'], vals['serial'])].add(1 if vals['quantity'] > 0 else -1)
        seen_serial_adds = set()
        running_deltas = defaultdict(float)
        for vals in rows:
            if not vals['is_valid']:
                continue
            if not vals['location_name'] or not vals['item_code'] or not vals['quantity']:
                vals.update(is_valid=False, error_msg=_('Missing data or zero qty'))
                continue
            product = products_by_code.get(vals['item_code'])
            location = locations_by_name.get(vals['location_name'])
            if not product or not location:
                vals.update(is_valid=False, error_msg=_('Product or location not found'))
                continue
            vals.update(product_id=product.id, location_id=location.id)
            if not product.is_storable or (product.company_id and product.company_id != self.company_id):
                vals.update(is_valid=False, error_msg=_('Product is not storable in this company'))
                continue
            lot = lots_by_key.get((product.id, vals['serial'])) if vals['serial'] else False
            if lot:
                vals['lot_id'] = lot.id
            if product.tracking == 'serial' and abs(vals['quantity']) != 1:
                vals.update(is_valid=False, error_msg=_('Serial qty must be 1 or -1'))
            elif product.tracking == 'none' and vals['serial']:
                vals.update(is_valid=False, error_msg=_('An untracked product cannot have a lot/serial'))
            elif product.tracking != 'none' and not vals['serial']:
                vals.update(is_valid=False, error_msg=_('Lot/serial number required'))
            elif vals['serial'] and not lot:
                vals.update(is_valid=False, error_msg='Serial Not Found')
            # A new serial has no stock. It must never inherit the product's
            # legacy stock without a serial when the lot does not exist yet.
            key = (product.id, location.id, lot.id if lot else vals['serial'] or False)
            previous = quantities[key] if lot or not vals['serial'] else 0
            if product.tracking == 'serial' and serial_signs[(vals['item_code'], vals['serial'])] == {1}:
                serial_key = (product.id, vals['serial'])
                note = self._serial_skip_note(location, previous, serial_positions.get(lot.id if lot else False),
                                              serial_key in seen_serial_adds)
                seen_serial_adds.add(serial_key)
                if note:
                    vals.update(skipped=True, result_note=note, previous_quantity=previous,
                                projected_quantity=previous + running_deltas[key])
                    continue
            running_deltas[key] += vals['quantity']
            vals.update(previous_quantity=previous, projected_quantity=previous + running_deltas[key])

        self.line_ids.unlink()
        self.env['import.stock.quant.line'].create(rows)
        self.write({'state': 'preview', 'applied_rows': 0,
                    'skipped_rows': sum(bool(r.get('skipped')) for r in rows), 'remaining_rows': len(rows),
                    'processing_error': False})
        _logger.info('Excel stock import %s: previewed %s rows in %.2fs', self.id, len(rows), perf_counter() - started)
        return self._open_wizard()

    def create_lots(self):
        self.ensure_one()
        self._create_missing_lots(self.line_ids)
        return self._open_wizard()

    def _create_missing_lots(self, lines):
        if self.state != 'preview':
            raise UserError(_('Preview the import first.'))
        self._check_import_company()
        candidates = lines.filtered(lambda line: line.serial and not line.lot_id and line.product_id
                                    and line.location_id and line.error_msg == 'Serial Not Found')
        Lot = self.env['stock.lot'].with_company(self.company_id)
        existing = Lot.search([
            ('product_id', 'in', candidates.product_id.ids), ('name', 'in', candidates.mapped('serial')),
            ('company_id', 'in', [self.company_id.id, False]),
        ])
        lots_by_key = {(lot.product_id.id, lot.name): lot for lot in existing.sorted(lambda lot: bool(lot.company_id))}
        missing_by_product = defaultdict(dict)
        for line in candidates:
            key = (line.product_id.id, line.serial)
            if key not in lots_by_key:
                missing_by_product[line.product_id.id][line.serial] = {
                    'name': line.serial, 'product_id': line.product_id.id, 'company_id': self.company_id.id,
                }
        # The installed serial uniqueness extension checks one product at a time.
        for values in missing_by_product.values():
            for lot in Lot.create(list(values.values())):
                lots_by_key[(lot.product_id.id, lot.name)] = lot
        lines_by_lot = defaultdict(lambda: self.env['import.stock.quant.line'])
        for line in candidates:
            lines_by_lot[lots_by_key[(line.product_id.id, line.serial)].id] |= line
        for lot_id, matching_lines in lines_by_lot.items():
            matching_lines.write({'lot_id': lot_id, 'is_valid': True, 'error_msg': 'Serial Created'})

    def action_remove_invalid_lines(self):
        self.ensure_one()
        if self.state != 'preview':
            raise UserError(_('Only a preview can be edited.'))
        self.line_ids.filtered(lambda l: not l.is_valid).unlink()
        self.remaining_rows = len(self.line_ids)

    def action_apply(self):
        self.ensure_one()
        self._check_import_company()
        self._lock_import()
        if self.state != 'preview' or not self.line_ids:
            raise UserError(_('Preview a non-empty import before applying it.'))
        if self.line_ids.filtered(lambda line: not line.is_valid):
            raise UserError(_('Fix errors before applying'))
        self._check_line_access(self.line_ids)
        if len(self.line_ids) > self._posting_batch_size:
            self.write({'state': 'processing', 'remaining_rows': len(self.line_ids), 'skipped_rows': 0})
            self.env.ref('import_stock_quant_excel.ir_cron_process_stock_import')._trigger()
        else:
            self._apply_lines(self.line_ids)
            self._update_progress()
        return self._open_wizard()

    def _lock_import(self):
        self.check_access('write')
        self.env.cr.execute('SELECT id FROM import_stock_quant_wizard WHERE id = %s FOR UPDATE', (self.id,))
        self.invalidate_recordset()

    def _check_line_access(self, lines):
        lines.product_id.check_access('read')
        lines.location_id.check_access('read')
        lines.lot_id.check_access('read')

    def _apply_lines(self, lines):
        started = perf_counter()
        self._check_import_company()
        self._check_line_access(lines)
        Quant = self.env['stock.quant'].sudo().with_company(self.company_id).with_context(
            to_date=False, disable_auto_svl=False, inventory_name=_('Excel stock adjustment: %s') % (self.filename or self.id))
        quants = Quant.search([
            ('product_id', 'in', lines.product_id.ids), ('location_id', 'in', lines.location_id.ids),
            ('company_id', '=', self.company_id.id), ('lot_id', 'in', lines.lot_id.ids + [False]),
        ])
        quants_by_key = defaultdict(lambda: Quant.browse())
        for quant in quants:
            quants_by_key[(quant.product_id.id, quant.location_id.id, quant.lot_id.id)] |= quant
        deltas = defaultdict(float)
        first_line_by_key = {}
        serial_positions = self._serial_positions(
            lines.lot_id.filtered(lambda lot: lot.product_id.tracking == 'serial'))
        serial_signs = defaultdict(set)
        for line in lines:
            if line.lot_id and line.quantity:
                serial_signs[(line.product_id.id, line.lot_id.id)].add(1 if line.quantity > 0 else -1)
        seen_serial_adds = set()
        skipped_by_note = defaultdict(lambda: self.env['import.stock.quant.line'])
        for line in lines:
            product, location, lot = line.product_id, line.location_id, line.lot_id
            if not product or not location:
                raise UserError(_('Every row requires a product and location.'))
            if not product.is_storable or (product.company_id and product.company_id != self.company_id):
                raise UserError(_('Select a storable product for this company.'))
            if location.usage != 'internal' or (location.company_id and location.company_id != self.company_id):
                raise UserError(_('Select an internal location in this company.'))
            if (product.tracking != 'none' and not lot) or (lot and (lot.product_id != product or
                    (lot.company_id and lot.company_id != self.company_id))) or (product.tracking == 'none' and lot):
                raise UserError(_('The row requires the matching lot/serial for this product and company.'))
            if not math.isfinite(line.quantity):
                raise UserError(_('Quantity must be a finite number.'))
            key = (product.id, location.id, lot.id)
            quant = quants_by_key[key]
            if len(quant) > 1 or any(q.owner_id or q.package_id for q in quant):
                raise UserError(_('Owned, packaged, or duplicate stock rows require individual inventory review.'))
            current = quant.quantity if quant else 0
            serial_key = (product.id, lot.id)
            if product.tracking == 'serial' and serial_signs[serial_key] == {1}:
                note = self._serial_skip_note(location, current, serial_positions.get(lot.id),
                                              serial_key in seen_serial_adds)
                seen_serial_adds.add(serial_key)
                if note:
                    skipped_by_note[note] |= line
                    continue
            if float_compare(current, line.previous_quantity, precision_rounding=product.uom_id.rounding):
                raise UserError(_('Stock changed since Preview at row %(row)s. Preview the file again.', row=line.row_no))
            deltas[key] += line.quantity
            first_line_by_key.setdefault(key, line)

        new_by_location = defaultdict(list)
        existing_by_target = defaultdict(lambda: Quant.browse())
        target_by_key = {}
        to_apply = Quant.browse()
        for key, delta in deltas.items():
            line = first_line_by_key[key]
            quant = quants_by_key[key]
            current = quant.quantity if quant else 0
            target = current + delta
            if not float_compare(delta, 0, precision_rounding=line.product_id.uom_id.rounding):
                continue
            if target < 0 or (line.product_id.tracking == 'serial' and target not in (0, 1)):
                raise UserError(_('Row %(row)s would create negative stock or more than one unit of a serial.', row=line.row_no))
            target_by_key[key] = target
            if quant:
                existing_by_target[target] |= quant
                to_apply |= quant
            else:
                new_by_location[line.location_id.id].append({
                    'product_id': line.product_id.id, 'location_id': line.location_id.id,
                    'lot_id': line.lot_id.id or False, 'inventory_quantity': target,
                })
        for target, matching_quants in existing_by_target.items():
            matching_quants.write({'inventory_quantity': target})
        # Group by location for the installed branch extension's multi-create.
        for values in new_by_location.values():
            to_apply |= Quant.with_context(inventory_mode=True).create(values)
        # One native posting operation creates stock moves, valuation layers and
        # accounting entries for the batch. Historical Inventory Issues remain
        # independent; do not lock every quant or scan every valuation per row.
        if to_apply and to_apply.action_apply_inventory():
            raise UserError(_('Stock changed during the import. Preview the file again before applying.'))
        to_apply.invalidate_recordset(['quantity'])
        for quant in to_apply:
            key = (quant.product_id.id, quant.location_id.id, quant.lot_id.id)
            if float_compare(quant.quantity, target_by_key[key], precision_rounding=quant.product_uom_id.rounding):
                raise UserError(_('The posted stock differs from the expected quantity. The import was not saved.'))

        skipped = self.env['import.stock.quant.line']
        for note, matching_lines in skipped_by_note.items():
            matching_lines.write({'skipped': True, 'result_note': note})
            skipped |= matching_lines
        (lines - skipped).write({'skipped': False, 'result_note': _('Processed')})
        lines.write({'processed': True})
        _logger.info('Excel stock import %s: processed %s rows in %.2fs', self.id, len(lines), perf_counter() - started)

    def _update_progress(self):
        # Count in PostgreSQL through the ORM rather than loading every row
        # again after each committed batch. Keep the importing user's rules.
        counts = self.env['import.stock.quant.line']._read_group(
            [('wizard_id', '=', self.id)], ['processed', 'skipped'], ['__count'])
        applied = skipped = remaining = 0
        for processed, is_skipped, count in counts:
            if not processed:
                remaining += count
            elif is_skipped:
                skipped += count
            else:
                applied += count
        self.write({'state': 'processing' if remaining else 'done',
                    'applied_rows': applied, 'skipped_rows': skipped, 'remaining_rows': remaining})

    @staticmethod
    def _batch_key(line):
        # Keep every row for the same serial together (including transfers),
        # and combine repeated untracked/lot rows before applying their delta.
        return (line.product_id.id, line.lot_id.id,
                False if line.product_id.tracking == 'serial' else line.location_id.id)

    def _next_batch(self):
        Line = self.env['import.stock.quant.line']
        pending_domain = [('wizard_id', '=', self.id), ('processed', '=', False)]
        seeds = Line.search(pending_domain, order='id', limit=self._posting_batch_size)
        if not seeds:
            return seeds
        # Fetch all partners of the first 100 pending rows, including a serial
        # transfer's opposite location or a repeated row far down the workbook.
        # A hard row limit here would change stock and valuation semantics.
        keys = {self._batch_key(line) for line in seeds}
        serial_lots = defaultdict(set)
        locations_by_lot = defaultdict(set)
        for product_id, lot_id, location_id in keys:
            if location_id:
                locations_by_lot[(product_id, lot_id)].add(location_id)
            else:
                serial_lots[product_id].add(lot_id)
        # A serial-heavy file commonly has 100 lots of the same product.
        # Use one IN clause for those lots instead of 100 OR branches.
        domains = [[('product_id', '=', product_id), ('lot_id', 'in', list(lot_ids))]
                   for product_id, lot_ids in serial_lots.items()]
        domains.extend([[('product_id', '=', product_id), ('lot_id', '=', lot_id),
                         ('location_id', 'in', list(location_ids))]
                        for (product_id, lot_id), location_ids in locations_by_lot.items()])
        return Line.search(expression.AND([pending_domain, expression.OR(domains)]), order='id')

    @api.model
    def _cron_process_imports(self):
        # SKIP LOCKED lets an already-running import finish without blocking
        # another worker. Each callback is committed by Odoo's cron runner.
        self.flush_model(['state'])
        self.env.cr.execute("""
            SELECT id FROM import_stock_quant_wizard WHERE state = 'processing'
            ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED
        """)
        row = self.env.cr.fetchone()
        done = 0
        if row:
            wizard = self.browse(row[0])
            worker = wizard.with_user(wizard.create_uid).with_company(wizard.company_id)
            try:
                with self.env.cr.savepoint():
                    worker._check_import_company()
                    batch = worker._next_batch()
                    worker._apply_lines(batch)
                    worker._update_progress()
                    done = len(batch)
            except Exception as exc:
                _logger.exception('Excel stock import %s paused; the current batch was rolled back', wizard.id)
                wizard.write({'state': 'failed', 'processing_error': str(exc)})
                done = 1  # Continue to other queued imports after this one pauses.
        remaining = self.env['import.stock.quant.line'].search_count([
            ('wizard_id.state', '=', 'processing'), ('processed', '=', False),
        ])
        self.env.ref('import_stock_quant_excel.ir_cron_process_stock_import')._notify_progress(
            done=done, remaining=remaining)

    def action_retry(self):
        self.ensure_one()
        self._lock_import()
        if self.state != 'failed':
            raise UserError(_('Only a paused import can be resumed.'))
        self._check_import_company()
        # Keep the original snapshots: Retry never silently re-adds completed
        # untracked rows or accepts stock movements after the preview.
        self.write({'state': 'processing', 'processing_error': False})
        self.env.ref('import_stock_quant_excel.ir_cron_process_stock_import')._trigger()
        return self._open_wizard()

    def action_refresh(self):
        self.ensure_one()
        return self._open_wizard()

    def _serial_positions(self, lots):
        positions = defaultdict(lambda: self.env['stock.location'])
        if lots:
            quants = self.env['stock.quant'].sudo().with_company(self.company_id).search([
                ('lot_id', 'in', lots.ids), ('company_id', '=', self.company_id.id),
                ('location_id.usage', 'in', ['internal', 'transit']), ('owner_id', '=', False), ('quantity', '>', 0),
            ])
            for quant in quants:
                positions[quant.lot_id.id] |= quant.location_id
        return positions

    def _serial_skip_note(self, location, current, positions, repeated):
        if current > 0:
            return _('Already in stock at %s; skipped') % location.complete_name
        if positions:
            return _('Already in stock at %s; use an internal transfer') % ', '.join(positions.mapped('complete_name'))
        if repeated:
            return _('Repeated serial in this file; skipped')
        return False

    def _check_import_company(self):
        if self.company_id not in self.env.user.company_ids:
            raise UserError(_('You cannot import stock for this company.'))

    @staticmethod
    def _excel_identifier(value):
        if value is None or value == '':
            return False
        if isinstance(value, (int, float)) and math.isfinite(value) and value == int(value):
            return str(int(value))
        return str(value).strip() or False

    def _open_wizard(self):
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id,
                'view_mode': 'form', 'target': 'new'}


class ImportStockQuantLine(models.Model):
    _name = 'import.stock.quant.line'
    _description = 'Import Stock Quant Line'

    wizard_id = fields.Many2one('import.stock.quant.wizard', ondelete='cascade', index=True)
    row_no = fields.Integer()
    location_name = fields.Char()
    item_code = fields.Char()
    quantity = fields.Float(string='Quantity to Add / Subtract')
    previous_quantity = fields.Float(string='Quantity Before', readonly=True)
    projected_quantity = fields.Float(string='Expected Quantity', readonly=True)
    serial = fields.Char()
    product_id = fields.Many2one('product.product')
    location_id = fields.Many2one('stock.location')
    lot_id = fields.Many2one('stock.lot')
    is_valid = fields.Boolean(default=True)
    error_msg = fields.Text()
    skipped = fields.Boolean(string='Skipped', readonly=True)
    result_note = fields.Char(string='Result / Note', readonly=True)
    processed = fields.Boolean(readonly=True, index=True)

    def create_lot(self):
        self.ensure_one()
        self.wizard_id._create_missing_lots(self)
        return self.wizard_id._open_wizard()



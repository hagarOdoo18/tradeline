from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError


class StockMultiUpdate(models.Model):
    _name = 'stock.multi.update'
    _description = 'Stock Multi Product Update'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char(
        string='Reference',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New'),
    )
    date = fields.Datetime(
        string='Date',
        required=True,
        default=fields.Datetime.now,
        tracking=True,
    )
    location_id = fields.Many2one(
        'stock.location',
        string='Location',
        required=True,
        domain=[('usage', '=', 'internal')],
        default=lambda self: self.env.ref('stock.stock_location_stock', raise_if_not_found=False),
        tracking=True,
    )
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company, readonly=True)
    notes = fields.Text(string='Notes')
    state = fields.Selection([
        ('draft', 'Draft'),
        ('done', 'Applied'),
        ('cancelled', 'Cancelled'),
    ], string='Status', default='draft', tracking=True, copy=False)

    line_ids = fields.One2many(
        'stock.multi.update.line',
        'update_id',
        string='Products',
        copy=True,
    )

    # Computed totals
    total_lines = fields.Integer(compute='_compute_totals', string='# Lines')
    total_add = fields.Float(compute='_compute_totals', string='Total Added', digits='Product Unit of Measure')
    total_sub = fields.Float(compute='_compute_totals', string='Total Subtracted', digits='Product Unit of Measure')

    @api.depends('line_ids.qty', 'line_ids.operation')
    def _compute_totals(self):
        for rec in self:
            rec.total_lines = len(rec.line_ids)
            rec.total_add = sum(l.qty for l in rec.line_ids if l.operation == 'add')
            rec.total_sub = sum(l.qty for l in rec.line_ids if l.operation == 'subtract')

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('stock.multi.update') or _('New')
        return super().create(vals_list)

    def action_apply(self):
        self.ensure_one()
        self.env.cr.execute('SELECT id FROM stock_multi_update WHERE id = %s FOR UPDATE', (self.id,))
        self.invalidate_recordset()
        if self.state != 'draft':
            raise UserError(_('Only draft records can be applied.'))
        if self.company_id != self.env.company:
            raise UserError(_('Switch to this stock update’s company before applying.'))
        if not self.line_ids:
            raise UserError(_('Please add at least one product line.'))

        for line in self.line_ids:
            line._validate()

        for product in self.line_ids.product_id.sorted('id'):
            product._lock_inventory_adjustment(self.env.company)
        for line in self.line_ids:
            line._apply_update()

        self.write({'state': 'done'})


    def action_cancel(self):
        for rec in self:
            if rec.state == 'done':
                raise UserError(_('Applied records cannot be cancelled.'))
            rec.state = 'cancelled'

    def action_reset_draft(self):
        for rec in self:
            if rec.state == 'cancelled':
                rec.state = 'draft'

    def action_print(self):
        return self.env.ref('stock_multi_update.action_report_stock_multi_update').report_action(self)

    def action_open_import_wizard(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Import Lines from Excel'),
            'res_model': 'stock.multi.update.import.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_update_id': self.id},
        }


class StockMultiUpdateLine(models.Model):
    _name = 'stock.multi.update.line'
    _description = 'Stock Multi Update Line'
    _order = 'sequence, id'

    update_id = fields.Many2one(
        'stock.multi.update',
        string='Update Order',
        required=True,
        ondelete='cascade',
        index=True,
    )
    sequence = fields.Integer(default=10)
    product_id = fields.Many2one(
        'product.product',
        string='Product',
        required=True,
        domain=[('type', '=', 'consu')],
        index=True,
    )
    product_uom_id = fields.Many2one(
        'uom.uom',
        string='Unit',
        related='product_id.uom_id',
        store=True,
        readonly=True,
    )
    tracking = fields.Selection(
        related='product_id.tracking',
        store=True,
        readonly=True,
    )
    lot_id = fields.Many2one(
        'stock.lot',
        string='Lot / Serial No.',
        domain="[('product_id', '=', product_id)]",
        context="{'default_product_id': product_id}",
    )
    lot_name = fields.Char(
        string='New Lot / Serial',
        help='Type a new lot or serial number to create it automatically.',
    )
    operation = fields.Selection([
        ('add', 'Add ➕'),
        ('subtract', 'Subtract ➖'),
    ], string='Operation', required=True, default='add')

    qty = fields.Float(
        string='Quantity',
        digits='Product Unit of Measure',
        required=True,
        default=1.0,
    )

    location_id = fields.Many2one(
        related='update_id.location_id',
        store=True,
        readonly=True,
    )
    state = fields.Selection(related='update_id.state', store=True)

    # ── Live preview (computed, not stored) ──────────────────────────────────
    qty_on_hand = fields.Float(
        string='Current Qty',
        compute='_compute_qty_preview',
        digits='Product Unit of Measure',
        help='Current quantity in stock at the selected location (live).',
    )
    qty_after_preview = fields.Float(
        string='Qty After (preview)',
        compute='_compute_qty_preview',
        digits='Product Unit of Measure',
        help='Estimated quantity after this update is applied.',
    )

    # ── Snapshot stored at apply time ────────────────────────────────────────
    qty_before = fields.Float(
        string='Qty Before',
        digits='Product Unit of Measure',
        readonly=True,
        copy=False,
        help='Actual quantity on hand captured the moment the update was applied.',
    )
    qty_after = fields.Float(
        string='Qty After',
        digits='Product Unit of Measure',
        readonly=True,
        copy=False,
        help='Actual quantity on hand after the update was applied.',
    )

    @api.depends('product_id', 'lot_id', 'lot_name', 'location_id', 'qty', 'operation', 'update_id.company_id')
    def _compute_qty_preview(self):
        for line in self:
            domain = [
                ('product_id', '=', line.product_id.id),
                ('location_id', '=', line.location_id.id),
            ]
            company = line.update_id.company_id or self.env.company
            lot = line.lot_id
            if not lot and line.lot_name and line.product_id:
                lot = self.env['stock.lot'].search([('name', '=', line.lot_name),
                    ('product_id', '=', line.product_id.id), ('company_id', 'in', [False, company.id])], limit=1)
            domain.extend([('lot_id', '=', lot.id or False), ('company_id', '=', company.id)])
            quants = self.env['stock.quant'].search(domain)
            current = sum(quants.mapped('quantity'))
            line.qty_on_hand = current
            delta = line.qty if line.operation == 'add' else -line.qty
            line.qty_after_preview = current + delta

    @api.onchange('product_id')
    def _onchange_product_id(self):
        self.lot_id = False
        self.lot_name = False
        self.qty = 1.0

    @api.onchange('lot_id')
    def _onchange_lot_id(self):
        if self.lot_name:
            self.lot_name = False

    @api.constrains('qty')
    def _check_qty(self):
        for line in self:
            if line.qty <= 0:
                raise ValidationError(_('Quantity must be greater than 0 for product "%s".') % line.product_id.display_name)

    def _validate(self):
        self.ensure_one()
        if self.tracking == 'serial':
            if not self.lot_id and not self.lot_name:
                raise UserError(
                    _('Product "%s" is tracked by serial number. Please provide a serial number.') % self.product_id.display_name
                )
            if self.qty != 1.0:
                raise UserError(
                    _('Product "%s" is tracked by serial. Quantity must be 1.') % self.product_id.display_name
                )
        elif self.tracking == 'lot':
            if not self.lot_id and not self.lot_name:
                raise UserError(
                    _('Product "%s" is tracked by lot. Please provide a lot number.') % self.product_id.display_name
                )
        # Check that subtract won't go negative
        if self.operation == 'subtract':
            if self.qty_after_preview < 0:
                raise UserError(
                    _('Cannot subtract %.2f from product "%s": only %.2f in stock at this location.')
                    % (self.qty, self.product_id.display_name, self.qty_on_hand)
                )


    def _apply_update(self):
        self.ensure_one()
        lot_id = False

        if self.tracking in ('serial', 'lot'):
            if self.lot_id:
                lot_id = self.lot_id.id
            elif self.lot_name:
                lot = self.env['stock.lot'].search([('name', '=', self.lot_name),
                    ('product_id', '=', self.product_id.id),
                    ('company_id', 'in', [False, self.env.company.id])], limit=1)
                if not lot:
                    lot = self.env['stock.lot'].create({
                        'name': self.lot_name,
                        'product_id': self.product_id.id,
                        'company_id': self.env.company.id,
                    })
                lot_id = lot.id

        product = self.product_id
        company = self.env.company
        product._lock_inventory_adjustment(company)
        quant = self.env['stock.quant'].search([
            ('product_id', '=', product.id), ('location_id', '=', self.location_id.id),
            ('company_id', '=', company.id), ('lot_id', '=', lot_id)])
        if len(quant) > 1:
            raise UserError(_('Duplicate stock rows require review.'))
        current = quant.quantity if quant else 0
        delta = self.qty if self.operation == 'add' else -self.qty
        before, after = product._apply_counted_inventory(
            company, self.location_id, current + delta,
            lot=self.env['stock.lot'].browse(lot_id),
            reason=_('Multi Stock Update %(ref)s: %(notes)s', ref=self.update_id.name, notes=self.update_id.notes or ''))
        self.write({'qty_before': before, 'qty_after': after})

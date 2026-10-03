# -*- coding: utf-8 -*-
import math

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import float_compare


class InventoryCountCostWizard(models.TransientModel):
    _name = 'stock.count.cost.wizard'
    _description = 'Count Stock and Set Unit Cost'

    state = fields.Selection([('draft', 'Draft'), ('preview', 'Preview'), ('done', 'Done')], default='draft', required=True)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company, required=True, readonly=True)
    product_id = fields.Many2one('product.product', required=True)
    location_id = fields.Many2one('stock.location', required=True)
    lot_id = fields.Many2one('stock.lot', string='Lot / Serial Number')
    tracking = fields.Selection(related='product_id.tracking', readonly=True)
    counted_quantity = fields.Float(string='Counted Quantity', required=True, digits='Product Unit of Measure',
                                    help='Enter the physical quantity now present at this location, not the change.')
    recorded_location_quantity = fields.Float(string='Recorded Quantity at Location',
        compute='_compute_recorded_location_quantity', digits='Product Unit of Measure',
        help='Odoo stock for this exact product, location and serial. Confirm it against an actual physical count.')
    change_cost = fields.Boolean(string='Set Unit Cost',
                                 help='For a valued serial/lot, this is its target unit cost. For an untracked product, '
                                      'this changes the company-wide product cost.')
    target_unit_cost = fields.Float(string='Target Cost per Unit', digits='Product Price',
        help='Cost of ONE unit, not total inventory value. For untracked products this revalues all company stock.')
    cost_source = fields.Char(string='Cost Source / Reference',
                              help='A PO, invoice, approved opening-stock estimate, or other source for the entered cost.')
    reason = fields.Text(required=True, help='Explain the count and cost source, such as a physical count and PO reference.')
    previous_quantity = fields.Float(readonly=True, digits='Product Unit of Measure')
    quantity_difference = fields.Float(readonly=True, digits='Product Unit of Measure')
    previous_product_quantity = fields.Float(readonly=True, digits='Product Unit of Measure')
    previous_product_value = fields.Monetary(readonly=True, currency_field='currency_id')
    previous_stored_cost = fields.Float(readonly=True, digits='Product Price')
    previous_unit_cost = fields.Float(readonly=True, digits='Product Price')
    projected_product_quantity = fields.Float(readonly=True, digits='Product Unit of Measure')
    projected_product_value = fields.Monetary(readonly=True, currency_field='currency_id')
    projected_product_cost = fields.Float(readonly=True, digits='Product Price')
    final_product_quantity = fields.Float(readonly=True, digits='Product Unit of Measure')
    final_product_value = fields.Monetary(readonly=True, currency_field='currency_id')
    final_product_cost = fields.Float(readonly=True, digits='Product Price')
    log_id = fields.Many2one('stock.count.cost.log', readonly=True)
    currency_id = fields.Many2one('res.currency', related='company_id.currency_id', readonly=True)

    @api.depends('product_id', 'location_id', 'lot_id', 'company_id')
    def _compute_recorded_location_quantity(self):
        for wizard in self:
            wizard.recorded_location_quantity = 0
            if wizard.product_id and wizard.location_id:
                quants = self.env['stock.quant'].search([
                    ('company_id', '=', wizard.company_id.id),
                    ('product_id', '=', wizard.product_id.id),
                    ('location_id', '=', wizard.location_id.id),
                    ('lot_id', '=', wizard.lot_id.id or False),
                    ('owner_id', '=', False)])
                wizard.recorded_location_quantity = sum(quants.mapped('quantity'))

    @api.onchange('product_id', 'location_id', 'lot_id')
    def _onchange_count_selection(self):
        if self.state == 'draft':
            if self.lot_id and self.lot_id.product_id != self.product_id:
                self.lot_id = False
            self.counted_quantity = max(0, self.recorded_location_quantity)

    def write(self, vals):
        inputs = {'product_id', 'location_id', 'lot_id', 'counted_quantity', 'change_cost', 'target_unit_cost', 'cost_source', 'reason', 'company_id'}
        if inputs.intersection(vals) and any(w.state == 'done' for w in self):
            raise UserError(_('Posted adjustments cannot be edited.'))
        if inputs.intersection(vals):
            vals = dict(vals, state='draft')
        return super().write(vals)

    def _check_operator(self):
        if not self.env.user.has_group('stock_valuation_layer_extend.group_count_cost_adjustment'):
            raise AccessError(_('You do not have access to controlled inventory adjustments.'))
        if self.company_id != self.env.company:
            raise UserError(_('Switch to the selected company in the Odoo company menu before adjusting stock.'))

    def _validate_inputs(self):
        self.ensure_one()
        self._check_operator()
        product = self.product_id.with_company(self.company_id).with_context(to_date=False, disable_auto_svl=False)
        rounding = product.uom_id.rounding
        if not math.isfinite(self.counted_quantity) or not math.isfinite(self.target_unit_cost):
            raise UserError(_('Quantity and cost must be finite numbers.'))
        if not product.is_storable or (product.company_id and product.company_id != self.company_id):
            raise UserError(_('Select a storable product belonging to the active company.'))
        if self.location_id.usage != 'internal' or (
                self.location_id.company_id and self.location_id.company_id != self.company_id):
            raise UserError(_('Select an internal location belonging to the active company.'))
        if product.tracking != 'none':
            if not self.lot_id or self.lot_id.product_id != product or (
                    self.lot_id.company_id and self.lot_id.company_id != self.company_id):
                raise UserError(_('Select the correct lot or serial number for this product and company.'))
        elif self.lot_id:
            raise UserError(_('An untracked product must not have a lot or serial number.'))
        if float_compare(self.counted_quantity, 0, precision_rounding=rounding) < 0:
            raise UserError(_('Counted quantity cannot be negative.'))
        if product.tracking == 'serial' and self.counted_quantity not in (0, 1):
            raise UserError(_('A serial number must have a counted quantity of 0 or 1.'))
        if not (self.reason or '').strip():
            raise UserError(_('Enter a reason and the source of the cost.'))
        if self.change_cost:
            if not self.env.user.has_group('stock_valuation_layer_extend.group_inventory_cost_approver'):
                raise AccessError(_('Only an Inventory Cost Approver can change unit costs.'))
            if not (self.cost_source or '').strip():
                raise UserError(_('Enter the source or approval reference for the unit cost.'))
            if product.cost_method != 'average' or product.valuation != 'real_time':
                raise UserError(_('Unit cost changes in this wizard require automated AVCO valuation.'))
            if product.tracking != 'none' and not product.lot_valuated:
                raise UserError(_('This tracked product is not valued by lot/serial. Use a manager valuation review.'))
            if float_compare(self.target_unit_cost, 0, precision_rounding=self.company_id.currency_id.rounding) <= 0:
                raise UserError(_('Target unit cost must be positive.'))
            if float_compare(self.counted_quantity, 0, precision_rounding=rounding) <= 0:
                raise UserError(_('A unit cost can only be set when the counted quantity is positive.'))
        if product.cost_method == 'average' and product.standard_price < 0:
            raise UserError(_('Product Cost is negative. Reconcile its valuation before adjusting stock.'))
        return product

    def _balances(self, product, final=False):
        Quant = self.env['stock.quant'].sudo().with_company(self.company_id).with_context(to_date=False, disable_auto_svl=False)
        domain = [
            ('product_id', '=', product.id),
            ('location_id', '=', self.location_id.id),
            ('lot_id', '=', self.lot_id.id if self.lot_id else False),
            ('company_id', '=', self.company_id.id),
        ]
        quants = Quant.search(domain)
        if any(q.owner_id or q.package_id for q in quants):
            raise UserError(_('This stock has an owner or package. Use Odoo Inventory Adjustments for that case.'))
        if len(quants) > 1:
            raise UserError(_('Several stock rows match this selection. Reconcile them before using the wizard.'))
        quant = quants[:1]
        quantity = quant.quantity if quant else 0.0
        if float_compare(quantity, 0, precision_rounding=product.uom_id.rounding) < 0:
            raise UserError(_('This stock row is negative. Reconcile it before using the wizard.'))
        if quant and quant.reserved_quantity and float_compare(
                quantity, self.counted_quantity, precision_rounding=product.uom_id.rounding):
            raise UserError(_('This stock is reserved. Release the reservation before changing its count.'))
        if self.lot_id and product.tracking == 'serial' and self.counted_quantity == 1:
            other_quants = Quant.search([
                ('product_id', '=', product.id), ('lot_id', '=', self.lot_id.id),
                ('company_id', '=', self.company_id.id), ('location_id', '!=', self.location_id.id),
                ('location_id.usage', 'in', ['internal', 'transit']), ('quantity', '>', 0),
            ])
            if other_quants:
                raise UserError(_(
                    'This serial is already in another internal or transit location. Use an internal transfer.'
                ))
        product._check_inventory_valuation_integrity(
            self.company_id, cost_only_lot=self.lot_id if self.change_cost else None)
        physical_quants = Quant.search([
            ('product_id', '=', product.id), ('company_id', '=', self.company_id.id),
            ('location_id.usage', 'in', ['internal', 'transit']), ('owner_id', '=', False),
        ])
        physical_quantity = sum(physical_quants.mapped('quantity'))
        valued_quantity = product.sudo().quantity_svl
        if float_compare(physical_quantity, valued_quantity, precision_rounding=product.uom_id.rounding):
            raise UserError(_(
                'This product already has a valuation quantity gap: physical %(physical)s, valuation %(valued)s. '
                'Reconcile it before another adjustment.', physical=physical_quantity, valued=valued_quantity,
            ))
        return {
            'stored_cost': self.lot_id.sudo().with_company(self.company_id).with_context(to_date=False, disable_auto_svl=False).standard_price
            if self.lot_id and product.lot_valuated else product.sudo().standard_price,
            'quant': quant,
            'quantity': quantity,
            'product_quantity': valued_quantity,
            'product_value': product.sudo().value_svl,
            'product_cost': product.sudo().standard_price,
            'unit_cost': self.lot_id.sudo().with_company(self.company_id).with_context(to_date=False, disable_auto_svl=False).avg_cost
            if self.lot_id and product.lot_valuated else product.sudo().standard_price,
        }

    def _open_self(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _('Count & Cost Adjustment'),
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_preview(self):
        self.ensure_one()
        product = self._validate_inputs()
        current = self._balances(product)
        difference = self.counted_quantity - current['quantity']
        if float_compare(difference, 0, precision_rounding=product.uom_id.rounding) > 0 and not self.change_cost:
            raise UserError(_('Enter a positive unit cost when adding stock. The source should be stated in Reason.'))
        if (not float_compare(difference, 0, precision_rounding=product.uom_id.rounding) and
                (not self.change_cost or not float_compare(
                    self.target_unit_cost, current['stored_cost'],
                    precision_rounding=self.company_id.currency_id.rounding) and not float_compare(
                    self.target_unit_cost, current['unit_cost'],
                    precision_rounding=self.company_id.currency_id.rounding))):
            raise UserError(_('Neither quantity nor cost would change.'))
        projected_qty = current['product_quantity'] + difference
        projected_value = current['product_value'] + difference * current['stored_cost']
        if self.change_cost:
            if self.lot_id:
                lot = self.lot_id.sudo().with_company(self.company_id).with_context(to_date=False, disable_auto_svl=False)
                projected_value = current['product_value'] - lot.value_svl + self.target_unit_cost * (lot.quantity_svl + difference)
            else:
                projected_value = self.target_unit_cost * projected_qty
        self.write({
            'projected_product_quantity': projected_qty,
            'projected_product_value': projected_value,
            'projected_product_cost': projected_value / projected_qty if projected_qty else 0,
            'state': 'preview',
            'previous_quantity': current['quantity'],
            'quantity_difference': difference,
            'previous_product_quantity': current['product_quantity'],
            'previous_product_value': current['product_value'],
            'previous_unit_cost': current['unit_cost'],
            'previous_stored_cost': current['stored_cost'],
        })
        return self._open_self()

    def action_edit(self):
        self.ensure_one()
        self._check_operator()
        if self.state == 'done':
            raise UserError(_('Posted adjustments cannot be reused.'))
        self.state = 'draft'
        return self._open_self()

    def action_apply(self):
        self.ensure_one()
        self.env.cr.execute('SELECT id FROM stock_count_cost_wizard WHERE id = %s FOR UPDATE', (self.id,))
        self.invalidate_recordset()
        if self.state != 'preview':
            raise UserError(_('Preview the adjustment before applying it.'))
        product = self._validate_inputs()
        product._lock_inventory_adjustment(self.company_id)
        current = self._balances(product)
        rounding = product.uom_id.rounding
        currency_rounding = self.company_id.currency_id.rounding
        if (float_compare(current['quantity'], self.previous_quantity, precision_rounding=rounding) or
                float_compare(current['product_quantity'], self.previous_product_quantity, precision_rounding=rounding) or
                float_compare(current['product_value'], self.previous_product_value, precision_rounding=currency_rounding) or
                float_compare(current['unit_cost'], self.previous_unit_cost, precision_rounding=currency_rounding) or
                float_compare(current['stored_cost'], self.previous_stored_cost, precision_rounding=currency_rounding)):
            raise UserError(_('Stock or valuation changed since the preview. Review the new balance and preview again.'))

        difference = self.counted_quantity - current['quantity']
        if float_compare(difference, 0, precision_rounding=rounding) > 0 and not self.change_cost:
            raise UserError(_('Enter a positive unit cost when adding stock.'))
        Log = self.env['stock.count.cost.log'].sudo()
        log = Log.create({
            'name': _('Controlled Adjustment - %(product)s', product=product.display_name),
            'company_id': self.company_id.id,
            'product_id': product.id,
            'location_id': self.location_id.id,
            'lot_id': self.lot_id.id if self.lot_id else False,
            'user_id': self.env.user.id,
            'performed_at': fields.Datetime.now(),
            'reason': self.reason.strip(),
            'cost_source': (self.cost_source or '').strip() if self.change_cost else False,
            'previous_quantity': current['quantity'],
            'counted_quantity': self.counted_quantity,
            'quantity_difference': difference,
            'previous_product_quantity': current['product_quantity'],
            'previous_product_value': current['product_value'],
            'previous_product_cost': current['product_cost'],
            'previous_unit_cost': current['unit_cost'],
            'previous_stored_cost': current['stored_cost'],
            'cost_changed': self.change_cost,
            'target_unit_cost': self.target_unit_cost if self.change_cost else 0,
        })
        Layer = self.env['stock.valuation.layer'].sudo().with_company(self.company_id).with_context(to_date=False, disable_auto_svl=False)
        before_layers = Layer.search([('product_id', '=', product.id), ('company_id', '=', self.company_id.id)])

        if float_compare(difference, 0, precision_rounding=rounding):
            quant = current['quant']
            if quant:
                quant.sudo().with_company(self.company_id).with_context(to_date=False, disable_auto_svl=False).inventory_quantity = self.counted_quantity
            else:
                quant = self.env['stock.quant'].sudo().with_company(self.company_id).with_context(to_date=False, disable_auto_svl=False).with_context(
                    inventory_mode=True).create({
                        'product_id': product.id,
                        'location_id': self.location_id.id,
                        'lot_id': self.lot_id.id if self.lot_id else False,
                        'inventory_quantity': self.counted_quantity,
                    })
            result = quant.sudo().with_company(self.company_id).with_context(to_date=False, disable_auto_svl=False).with_context(
                inventory_name=_('Controlled Adjustment %(number)s: %(reason)s',
                                 number=log.id, reason=self.reason.strip()[:80]),
            ).action_apply_inventory()
            if result:
                raise UserError(_('Odoo requires inventory conflict review. No change was posted.'))

        if self.change_cost:
            if self.lot_id:
                self.lot_id.sudo().with_company(self.company_id).with_context(to_date=False, disable_auto_svl=False).write({
                    'standard_price': self.target_unit_cost,
                })
            else:
                product.sudo().write({'standard_price': self.target_unit_cost})

        final = self._balances(product, final=True)
        if float_compare(final['product_quantity'], self.projected_product_quantity, precision_rounding=rounding):
            raise UserError(_('The resulting valuation quantity differs from the preview. No adjustment was saved.'))
        if self.change_cost and float_compare(final['product_value'], self.projected_product_value, precision_rounding=currency_rounding):
            raise UserError(_('The resulting valuation value differs from the preview. No adjustment was saved.'))
        if float_compare(final['quantity'], self.counted_quantity, precision_rounding=rounding):
            raise UserError(_('The final counted quantity differs from the request. No change was posted.'))
        if self.change_cost and (float_compare(
                final['unit_cost'], self.target_unit_cost, precision_rounding=currency_rounding) or float_compare(
                final['stored_cost'], self.target_unit_cost, precision_rounding=currency_rounding)):
            raise UserError(_('The final unit cost differs from the request. No change was posted.'))
        layers = Layer.search([('product_id', '=', product.id), ('company_id', '=', self.company_id.id)]) - before_layers
        if any(layer.value and (not layer.account_move_id or layer.account_move_id.state != 'posted') for layer in layers):
            raise UserError(_('Required valuation accounting entries were not posted. No adjustment was saved.'))
        if product.cost_method == 'average' and final['product_quantity'] > 0 and float_compare(
                final['product_cost'], final['product_value'] / final['product_quantity'], precision_rounding=currency_rounding):
            raise UserError(_('The resulting Product Cost does not match its valuation. No adjustment was saved.'))
        log.write({
            'final_unit_cost': final['stored_cost'],
            'final_product_quantity': final['product_quantity'],
            'final_product_value': final['product_value'],
            'final_product_cost': final['product_cost'],
            'valuation_layer_ids': [(6, 0, layers.ids)],
        })
        self.write({
            'state': 'done',
            'final_product_quantity': final['product_quantity'],
            'final_product_value': final['product_value'],
            'final_product_cost': final['product_cost'],
            'log_id': log.id,
        })
        return self._open_self()

    def action_open_log(self):
        self.ensure_one()
        self._check_operator()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Adjustment Record'),
            'res_model': 'stock.count.cost.log',
            'res_id': self.log_id.id,
            'view_mode': 'form',
            'target': 'current',
        }

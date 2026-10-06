import math

from odoo import _, fields, models, Command
from odoo.exceptions import AccessError, UserError
from odoo.tools import float_compare


class ValuationReconciliationWizard(models.TransientModel):
    _name = 'stock.valuation.reconciliation.wizard'
    _description = 'Reconcile Valuation to Verified Stock'

    state = fields.Selection([('draft', 'Draft'), ('preview', 'Preview'), ('done', 'Done')], default='draft', required=True)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company, required=True, readonly=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    product_id = fields.Many2one('product.product', required=True, readonly=True)
    lot_id = fields.Many2one('stock.lot', readonly=True)
    value_policy = fields.Selection([('keep', 'Keep existing total value'), ('cost', 'Set verified cost per unit')], required=True, default='keep')
    target_unit_cost = fields.Float(string='Verified Cost per Unit', digits='Product Price')
    counterpart_account_id = fields.Many2one('account.account', string='Correction Counterpart Account')
    confirmed = fields.Boolean(string='I verified that the recorded physical stock is correct', default=True)
    reason = fields.Text(default='Administrator valuation review')
    source = fields.Char(string='Count / Cost Evidence Reference', default='Administrator review')
    parent_id = fields.Many2one('stock.valuation.reconciliation.wizard', ondelete='cascade', readonly=True)
    detail_ids = fields.One2many('stock.valuation.reconciliation.wizard', 'parent_id', readonly=True)
    physical_quantity = fields.Float(readonly=True, digits='Product Unit of Measure')
    valuation_quantity = fields.Float(readonly=True, digits='Product Unit of Measure')
    valuation_value = fields.Monetary(readonly=True)
    correction_quantity = fields.Float(readonly=True, digits='Product Unit of Measure')
    correction_value = fields.Monetary(readonly=True)
    resulting_value = fields.Monetary(readonly=True)
    resulting_cost = fields.Float(readonly=True, digits='Product Price')
    pending_negative_quantity = fields.Float(readonly=True, digits='Product Unit of Measure')
    snapshot = fields.Json(readonly=True)
    correction_layer_id = fields.Many2one('stock.valuation.layer', readonly=True)

    def write(self, vals):
        inputs = {'value_policy', 'target_unit_cost', 'counterpart_account_id', 'confirmed', 'reason', 'source', 'product_id', 'lot_id', 'company_id'}
        if inputs.intersection(vals):
            if any(w.state == 'done' for w in self):
                raise UserError(_('Posted reconciliations cannot be edited.'))
            vals = dict(vals, state='draft')
        return super().write(vals)

    def _check_reconciliation_operator(self):
        self.ensure_one()
        if not self.env.user.has_group('stock_valuation_layer_extend.group_stock_valuation_quantity_correction'):
            raise AccessError(_('A valuation reconciliation manager is required.'))
        if self.company_id != self.env.company or self.company_id not in self.env.user.company_ids:
            raise UserError(_('Switch to the reconciliation company first.'))
        p = self.product_id.with_company(self.company_id).with_context(to_date=False)
        p.check_access('read')
        if not p.is_storable or (p.company_id and p.company_id != self.company_id):
            raise UserError(_('Select a storable product in this company.'))
        if p.cost_method != 'average' or p.valuation != 'real_time':
            raise UserError(_('This repair supports automated average-cost valuation only.'))
        if self.lot_id:
            if self.lot_id.product_id != p or (self.lot_id.company_id and self.lot_id.company_id != self.company_id):
                raise UserError(_('The automatic serial selection belongs to another product or company.'))
            self.lot_id.check_access('read')
        return p

    def _read_balances(self, p):
        domain = [('company_id', '=', self.company_id.id), ('product_id', '=', p.id)]
        # For an untracked product include every layer; a valued product must use its exact lot.
        if self.lot_id or self.parent_id:
            domain.append(('lot_id', '=', self.lot_id.id or False))
        layers = self.env['stock.valuation.layer'].sudo().search(domain, order='id')
        quants = self.env['stock.quant'].sudo().search(domain + [
            ('location_id.usage', 'in', ['internal', 'transit']), ('owner_id', '=', False)], order='id')
        physical = sum(quants.mapped('quantity'))
        return {'physical': physical, 'quantity': sum(layers.mapped('quantity')), 'value': sum(layers.mapped('value')),
                'quants': [[q.id, q.quantity, q.location_id.id, q.lot_id.id, q.package_id.id] for q in quants],
                'layers': [[s.id, s.quantity, s.value, s.remaining_qty, s.remaining_value] for s in layers]}

    def _projection(self, current):
        qty = current['physical']
        if not math.isfinite(self.target_unit_cost) or (self.value_policy == 'cost' and self.target_unit_cost < 0):
            raise UserError(_('Unit cost must be a finite, nonnegative number.'))
        if qty < 0:
            raise UserError(_('The total recorded physical quantity is negative. Correct the stock count before valuing it.'))
        value = current['value'] if self.value_policy == 'keep' else self.company_id.currency_id.round(qty * self.target_unit_cost)
        if qty == 0:
            value = 0
        delta = qty - current['quantity']
        return delta, self.company_id.currency_id.round(value - current['value']), value, value / qty if qty else 0

    def _accounting_settings(self, p, delta_value):
        if self.company_id.currency_id.is_zero(delta_value):
            return None, None
        cat = p.categ_id
        stock_account = cat.property_stock_valuation_account_id
        journal = cat.property_stock_journal
        other = self.counterpart_account_id
        if not other:
            # Use the configured stock counterpart, never an arbitrary ledger account.
            other = cat.property_stock_account_output_categ_id or p.property_account_expense_id or cat.property_account_expense_categ_id
            self.counterpart_account_id = other
        if not journal or journal.company_id != self.company_id or journal.type != 'general' or (journal.currency_id and journal.currency_id != self.company_id.currency_id):
            raise UserError(_('Configure the company stock valuation journal.'))
        if not stock_account or self.company_id not in stock_account.company_ids or stock_account.deprecated:
            raise UserError(_('Configure an active company stock valuation account.'))
        if not other or other == stock_account or self.company_id not in other.company_ids or other.deprecated or other.account_type in ('asset_receivable', 'liability_payable', 'off_balance'):
            raise UserError(_('Select an active company counterpart account, different from the valuation account. Receivable/payable accounts are unsupported.'))
        return journal, stock_account

    def _open_self(self):
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id,
                'view_mode': 'form', 'target': 'new', 'name': _('Reconcile Valuation to Verified Stock')}

    def action_preview(self):
        p = self._check_reconciliation_operator()
        current = self._read_balances(p)
        dq, dv, value, cost = self._projection(current)
        if p.lot_valuated and p.tracking != 'none' and not self.lot_id and not self.parent_id:
            self.detail_ids.unlink()
            lot_ids = sorted({q[3] or 0 for q in current['quants']} | {
                s.lot_id.id or 0 for s in self.env['stock.valuation.layer'].sudo().browse([s[0] for s in current['layers']])})
            for lot_id in lot_ids:
                detail = self.create({'parent_id': self.id, 'company_id': self.company_id.id,
                    'product_id': p.id, 'lot_id': lot_id or False, 'value_policy': self.value_policy,
                    'target_unit_cost': self.target_unit_cost, 'counterpart_account_id': self.counterpart_account_id.id,
                    'source': self.source, 'reason': self.reason})
                detail.action_preview()
            dv = sum(self.detail_ids.mapped('correction_value'))
            value = sum(self.detail_ids.mapped('resulting_value'))
            cost = value / current['physical'] if current['physical'] else 0
        else:
            self._accounting_settings(p, dv)
        self.write({'physical_quantity': current['physical'], 'valuation_quantity': current['quantity'],
                    'valuation_value': current['value'], 'correction_quantity': dq, 'correction_value': dv,
                    'resulting_value': value, 'resulting_cost': cost,
                    'pending_negative_quantity': sum(s[3] for s in current['layers'] if s[3] < 0),
                    'snapshot': current, 'state': 'preview'})
        return self._open_self()

    def action_apply(self):
        self.ensure_one()
        self.env.cr.execute('SELECT id FROM stock_valuation_reconciliation_wizard WHERE id=%s FOR UPDATE', (self.id,))
        self.invalidate_recordset()
        p = self._check_reconciliation_operator()
        if self.state != 'preview':
            raise UserError(_('Preview this reconciliation before posting.'))
        p._lock_inventory_adjustment(self.company_id)
        current = self._read_balances(p)
        if current != self.snapshot:
            raise UserError(_('Stock or valuation changed since preview. Preview again.'))
        if self.detail_ids:
            with self.env.cr.savepoint():
                for detail in self.detail_ids:
                    detail.action_apply()
                self.env.invalidate_all()
                after = self._read_balances(p)
                if after['quants'] != current['quants'] or float_compare(after['quantity'], current['physical'], precision_rounding=p.uom_id.rounding) or not self.company_id.currency_id.is_zero(after['value'] - self.resulting_value):
                    raise UserError(_('Valuation verification failed. Nothing was posted.'))
                self.write({'state': 'done'})
            return self._open_self()
        dq, dv, value, cost = self._projection(current)
        journal, stock_account = self._accounting_settings(p, dv)
        # A savepoint makes journal, layer, cache and post-checks atomic even if an API caller catches the error.
        with self.env.cr.savepoint():
            description = _('Valuation reconciliation: %(product)s / %(lot)s; %(reason)s; evidence: %(evidence)s',
                            product=p.display_name, lot=self.lot_id.display_name or '-', reason=(self.reason or 'Administrator review').strip(), evidence=(self.source or 'Administrator review').strip())
            move = self.env['account.move']
            if journal:
                move = move.sudo().with_company(self.company_id).create({
                    'move_type': 'entry', 'company_id': self.company_id.id, 'journal_id': journal.id,
                    'date': fields.Date.context_today(self), 'ref': description,
                    'line_ids': [Command.create({'name': description, 'account_id': account.id,
                                                'product_id': p.id, 'debit': max(amount, 0), 'credit': max(-amount, 0)})
                                 for account, amount in [(stock_account, dv), (self.counterpart_account_id, -dv)]]})
                move.action_post()
                if move.state != 'posted':
                    raise UserError(_('The correction journal was not posted.'))
            layer = self.env['stock.valuation.layer'].sudo().create({
                'company_id': self.company_id.id, 'product_id': p.id, 'lot_id': self.lot_id.id,
                'quantity': dq, 'value': dv, 'unit_cost': dv / dq if dq else 0,
                'remaining_qty': current['physical'], 'remaining_value': value, 'description': description,
                'account_move_id': move.id, 'is_valuation_reconciliation': True,
                'reconciliation_physical_quantity': current['physical'],
                'reconciliation_previous_quantity': current['quantity'],
                'reconciliation_previous_value': current['value'],
                'reconciliation_policy': self.value_policy,
                'reconciliation_user_id': self.env.user.id,
                'reconciliation_evidence': (self.source or 'Administrator review').strip(),
                'reconciliation_layer_snapshot': current['layers']})
            # AVCO also consumes FIFO candidates and vacuums negative remaining layers.
            # Rebase the technical pool so a future receipt cannot settle phantom negative
            # stock a second time. Posted source quantities/values and stock moves stay intact.
            source_layers = self.env['stock.valuation.layer'].sudo().browse([s[0] for s in current['layers']])
            source_layers.write({'remaining_qty': 0, 'remaining_value': 0})
            if move:
                move.write({'stock_valuation_layer_ids': [Command.link(layer.id)]})
                stock_line = move.line_ids.filtered(lambda line: line.account_id == stock_account)
                if len(stock_line) != 1 or not self.company_id.currency_id.is_zero(stock_line.balance - dv):
                    raise UserError(_('The posted journal does not match the valuation correction.'))
                layer.account_move_line_id = stock_line.id
            self.env.invalidate_all()
            after = self._read_balances(p)
            if after['quants'] != current['quants'] or float_compare(after['quantity'], current['physical'], precision_rounding=p.uom_id.rounding) or not self.company_id.currency_id.is_zero(after['value'] - value):
                raise UserError(_('Reconciliation verification failed. Nothing was posted.'))
            if float_compare(sum(s[3] for s in after['layers']), current['physical'], precision_rounding=p.uom_id.rounding) or not self.company_id.currency_id.is_zero(sum(s[4] for s in after['layers']) - value):
                raise UserError(_('The remaining valuation balance failed verification. Nothing was posted.'))
            # Clearing orphan value at zero physical stock is a value-only repair.
            # Preserve the serial/lot's stored cost cache when no units remain.
            if self.lot_id and current['physical'] > 0:
                self.lot_id.sudo().with_company(self.company_id).with_context(disable_auto_svl=True).write({'standard_price': cost})
            p._sync_standard_price_from_valuation(self.company_id)
            self.write({'correction_layer_id': layer.id, 'state': 'done'})
        return self._open_self()

    def action_edit(self):
        self._check_reconciliation_operator()
        if self.state == 'done':
            raise UserError(_('Posted reconciliations cannot be reused.'))
        self.state = 'draft'
        return self._open_self()

# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_compare, float_is_zero


class StockValuationLayerNeutralizeWizard(models.TransientModel):
    _name = 'stock.valuation.layer.neutralize.wizard'
    _description = 'Neutralize Erroneous Valuation Quantity Update'

    source_svl_id = fields.Many2one(
        'stock.valuation.layer',
        string='Erroneous Valuation Layer',
        required=True,
        readonly=True,
    )
    company_id = fields.Many2one(related='source_svl_id.company_id', readonly=True)
    currency_id = fields.Many2one(related='company_id.currency_id', readonly=True)
    product_id = fields.Many2one(related='source_svl_id.product_id', readonly=True)
    lot_id = fields.Many2one(related='source_svl_id.lot_id', readonly=True)
    current_physical_qty = fields.Float(
        string='Current Physical Quantity',
        digits='Product Unit of Measure',
        readonly=True,
    )
    current_valuation_qty = fields.Float(
        string='Current Valuation Quantity',
        digits='Product Unit of Measure',
        readonly=True,
    )
    current_valuation_value = fields.Monetary(
        string='Current Valuation Value',
        currency_field='currency_id',
        readonly=True,
    )
    correction_quantity = fields.Float(
        string='Correction Quantity',
        digits='Product Unit of Measure',
        readonly=True,
    )
    correction_value = fields.Monetary(
        string='Correction Value',
        currency_field='currency_id',
        readonly=True,
    )
    resulting_valuation_qty = fields.Float(
        string='Resulting Valuation Quantity',
        digits='Product Unit of Measure',
        readonly=True,
    )
    resulting_valuation_value = fields.Monetary(
        string='Resulting Valuation Value',
        currency_field='currency_id',
        readonly=True,
    )
    reason = fields.Text(required=True)
    confirmation = fields.Boolean(
        string='I confirm that this serial is not physically in stock and the selected quantity update is erroneous.',
    )

    @api.model
    def default_get(self, field_names):
        values = super().default_get(field_names)
        active_ids = self.env.context.get('active_ids', [])
        if self.env.context.get('active_model') != 'stock.valuation.layer' or len(active_ids) != 1:
            raise UserError(_('Select exactly one valuation layer.'))

        source = self.env['stock.valuation.layer'].browse(active_ids).exists()
        if not source:
            raise UserError(_('The selected valuation layer no longer exists.'))

        totals = self._get_serial_totals(source)
        values.update({
            'source_svl_id': source.id,
            **totals,
            'correction_quantity': -totals['current_valuation_qty'],
            'correction_value': -totals['current_valuation_value'],
            'resulting_valuation_qty': 0.0,
            'resulting_valuation_value': 0.0,
        })
        return values

    @api.model
    def _get_serial_totals(self, source):
        if not source.lot_id:
            return {
                'current_physical_qty': 0.0,
                'current_valuation_qty': 0.0,
                'current_valuation_value': 0.0,
            }

        domain = [
            ('company_id', '=', source.company_id.id),
            ('product_id', '=', source.product_id.id),
            ('lot_id', '=', source.lot_id.id),
        ]
        valuation_layers = self.env['stock.valuation.layer'].sudo().search(domain)
        internal_quants = self.env['stock.quant'].sudo().search([
            ('company_id', '=', source.company_id.id),
            ('product_id', '=', source.product_id.id),
            ('lot_id', '=', source.lot_id.id),
            ('location_id.usage', '=', 'internal'),
        ])
        return {
            'current_physical_qty': sum(internal_quants.mapped('quantity')),
            'current_valuation_qty': sum(valuation_layers.mapped('quantity')),
            'current_valuation_value': sum(valuation_layers.mapped('value')),
        }

    def _validate_source(self, source, totals):
        self.ensure_one()
        precision = source.product_id.uom_id.rounding
        reference = (source.reference or source.stock_move_id.reference or '').strip()

        if reference != 'Product Quantity Updated':
            raise ValidationError(_('Only a Product Quantity Updated valuation layer can be neutralized.'))
        if not source.stock_move_id or source.stock_move_id.state != 'done':
            raise ValidationError(_('The selected valuation layer must belong to a completed stock move.'))
        if not source.lot_id or source.product_id.tracking != 'serial':
            raise ValidationError(_('The selected layer must belong to a serial-tracked product and serial number.'))
        if float_compare(source.quantity, -1.0, precision_rounding=precision) != 0:
            raise ValidationError(_('The selected erroneous quantity update must have quantity -1.'))
        if not float_is_zero(totals['current_physical_qty'], precision_rounding=precision):
            raise ValidationError(_(
                'The serial currently has physical quantity %(quantity)s. It must be zero before a valuation-only correction.',
                quantity=totals['current_physical_qty'],
            ))
        if float_compare(totals['current_valuation_qty'], -1.0, precision_rounding=precision) != 0:
            raise ValidationError(_(
                'The serial valuation quantity is %(quantity)s, not -1. This case requires manual review.',
                quantity=totals['current_valuation_qty'],
            ))
        if float_compare(
            totals['current_valuation_value'],
            0.0,
            precision_rounding=source.company_id.currency_id.rounding,
        ) > 0:
            raise ValidationError(_(
                'The serial has a positive valuation value. This case requires manual accounting review.',
            ))
        if self.env['stock.valuation.layer'].sudo().search_count([
            ('quantity_neutralization_source_id', '=', source.id),
        ]):
            raise ValidationError(_('This valuation layer has already been neutralized.'))

    def action_confirm(self):
        self.ensure_one()
        if not self.confirmation:
            raise ValidationError(_('You must confirm the physical-stock check.'))
        if not (self.reason or '').strip():
            raise ValidationError(_('A correction reason is required.'))

        source = self.source_svl_id.exists()
        if not source:
            raise UserError(_('The selected valuation layer no longer exists.'))

        totals = self._get_serial_totals(source)
        self._validate_source(source, totals)

        correction_qty = -totals['current_valuation_qty']
        correction_value = -totals['current_valuation_value']
        description = _(
            'Neutralization of erroneous Product Quantity Updated for %(serial)s; source SVL %(source)s; reason: %(reason)s',
            serial=source.lot_id.display_name,
            source=source.id,
            reason=self.reason.strip(),
        )
        correction_svl = self.env['stock.valuation.layer'].sudo().create({
            'company_id': source.company_id.id,
            'product_id': source.product_id.id,
            'lot_id': source.lot_id.id,
            'quantity': correction_qty,
            'unit_cost': abs(correction_value / correction_qty) if correction_qty else 0.0,
            'value': correction_value,
            'remaining_qty': 0.0,
            'remaining_value': 0.0,
            'description': description,
            'stock_valuation_layer_id': source.id,
            'is_quantity_neutralization': True,
            'quantity_neutralization_source_id': source.id,
            'quantity_neutralization_reason': self.reason.strip(),
            'quantity_neutralization_user_id': self.env.user.id,
            'quantity_neutralization_date': fields.Datetime.now(),
        })
        correction_svl._validate_accounting_entries()

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Valuation Quantity Corrected'),
                'message': _(
                    '%(serial)s now has valuation quantity 0 and value 0. Physical stock was not changed.',
                    serial=source.lot_id.display_name,
                ),
                'type': 'success',
                'sticky': True,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }

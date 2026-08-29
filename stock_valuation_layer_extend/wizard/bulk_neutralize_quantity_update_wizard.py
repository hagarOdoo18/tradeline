# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class StockValuationLayerBulkNeutralizeWizard(models.TransientModel):
    _name = 'stock.valuation.layer.bulk.neutralize.wizard'
    _description = 'Bulk Neutralize Erroneous Valuation Quantity Updates'

    source_svl_ids = fields.Many2many(
        'stock.valuation.layer',
        string='Selected Valuation Layers',
        required=True,
        readonly=True,
    )
    candidate_count = fields.Integer(readonly=True)
    company_id = fields.Many2one('res.company', readonly=True)
    currency_id = fields.Many2one(related='company_id.currency_id', readonly=True)
    total_correction_qty = fields.Float(
        string='Total Correction Quantity',
        digits='Product Unit of Measure',
        readonly=True,
    )
    total_correction_value = fields.Monetary(
        string='Total Correction Value',
        currency_field='currency_id',
        readonly=True,
    )
    reason = fields.Text(required=True)
    confirmation = fields.Boolean(
        string='I reviewed every selected serial and authorize the valuation and accounting corrections.',
    )

    @api.model
    def default_get(self, field_names):
        values = super().default_get(field_names)
        active_ids = self.env.context.get('active_ids', [])
        if self.env.context.get('active_model') != 'stock.valuation.quantity.correction.candidate' or not active_ids:
            raise UserError(_('Select at least one valuation correction candidate.'))

        candidates = self.env['stock.valuation.quantity.correction.candidate'].browse(active_ids).exists()
        if len(candidates) > 100:
            raise ValidationError(_('A maximum of 100 corrections can be processed in one batch.'))
        companies = candidates.mapped('company_id')
        if len(companies) != 1:
            raise ValidationError(_('All selected candidates must belong to the same company.'))

        values.update({
            'source_svl_ids': [(6, 0, candidates.mapped('source_svl_id').ids)],
            'candidate_count': len(candidates),
            'company_id': companies.id,
            'total_correction_qty': sum(candidates.mapped('correction_qty')),
            'total_correction_value': sum(candidates.mapped('correction_value')),
        })
        return values

    def action_confirm(self):
        self.ensure_one()
        if not self.confirmation:
            raise ValidationError(_('You must confirm the reviewed batch.'))
        if not (self.reason or '').strip():
            raise ValidationError(_('A batch correction reason is required.'))
        if not self.source_svl_ids:
            raise ValidationError(_('No candidates were selected.'))

        single_wizard_model = self.env['stock.valuation.layer.neutralize.wizard']
        single_fields = [
            'source_svl_id',
            'current_physical_qty',
            'current_valuation_qty',
            'current_valuation_value',
            'correction_quantity',
            'correction_value',
            'resulting_valuation_qty',
            'resulting_valuation_value',
        ]
        corrected_serials = []
        for source in self.source_svl_ids.sorted('id'):
            source = source.exists()
            if not source:
                raise ValidationError(_('A selected source valuation layer no longer exists.'))
            context = {
                **self.env.context,
                'active_model': 'stock.valuation.layer',
                'active_id': source.id,
                'active_ids': [source.id],
            }
            wizard_env = single_wizard_model.with_context(context)
            values = wizard_env.default_get(single_fields)
            values.update({
                'reason': _(
                    'Batch correction: %(batch_reason)s',
                    batch_reason=self.reason.strip(),
                ),
                'confirmation': True,
            })
            wizard_env.create(values).action_confirm()
            corrected_serials.append(source.lot_id.display_name)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Valuation Corrections Completed'),
                'message': _(
                    '%(count)s serial valuation balances were neutralized. Physical stock was not changed.',
                    count=len(corrected_serials),
                ),
                'type': 'success',
                'sticky': True,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }

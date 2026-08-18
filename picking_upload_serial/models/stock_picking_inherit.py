# -*- coding: utf-8 -*-
import logging

from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    upload_validate_pending = fields.Boolean(
        string='Validation Queued',
        copy=False,
        index=True,
        help="Set by the Excel upload wizard when validation is deferred to "
             "the background job.",
    )
    upload_validate_error = fields.Text(
        string='Background Validation Error',
        readonly=True,
        copy=False,
    )

    def action_open_upload_excel_wizard(self):
        """Open upload wizard for the current delivery order"""
        return {
            'name': _('Upload Delivery Excel'),
            'type': 'ir.actions.act_window',
            'res_model': 'upload.delivery.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_picking_id': self.id,
            }
        }

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------
    def _upload_validate_now(self, auto_backorder=False):
        """button_validate with the overhead switched off.

        `tracking_disable` skips the chatter tracking that Odoo would
        otherwise compute for every write done during validation, which is
        where a large part of the time goes on pickings with thousands of
        operation lines.

        auto_backorder=False -> the backorder confirmation action is returned
        to the caller (the user still gets the popup).
        auto_backorder=True  -> the backorder is created without any popup
        (used by the background job, which has nobody to ask).
        """
        self.ensure_one()

        picking = self.with_context(tracking_disable=True, skip_sms=True)
        res = picking.button_validate()

        if auto_backorder and isinstance(res, dict) \
                and res.get('res_model') == 'stock.backorder.confirmation':
            ctx = dict(res.get('context') or {})
            wizard = self.env['stock.backorder.confirmation'].with_context(**ctx).create({})
            wizard.process()
            return True

        return res

    def action_queue_upload_validation(self):
        """Flag the picking so the cron validates it out of the user's way."""
        self.write({
            'upload_validate_pending': True,
            'upload_validate_error': False,
        })
        return True

    @api.model
    def _cron_validate_uploaded_pickings(self, limit=5):
        """Validate the pickings queued by the Excel upload wizard.

        One savepoint per picking: a picking that cannot be validated stores
        its error and is unqueued, it never blocks the others.
        """
        pickings = self.search([
            ('upload_validate_pending', '=', True),
            ('state', 'not in', ('done', 'cancel')),
        ], limit=limit)

        for picking in pickings:
            try:
                with self.env.cr.savepoint():
                    picking._upload_validate_now(auto_backorder=True)
                    picking.write({
                        'upload_validate_pending': False,
                        'upload_validate_error': False,
                    })
            except Exception as e:
                _logger.exception(
                    'Background validation failed for picking %s', picking.name)
                picking.write({
                    'upload_validate_pending': False,
                    'upload_validate_error': str(e),
                })
                picking.message_post(
                    body=_("Background validation failed:<br/>%s") % str(e))

            # commit picking by picking so a later failure cannot undo the
            # transfers already validated in this run
            self.env.cr.commit()

        # unqueue anything that got validated or cancelled elsewhere
        stale = self.search([
            ('upload_validate_pending', '=', True),
            ('state', 'in', ('done', 'cancel')),
        ])
        if stale:
            stale.write({'upload_validate_pending': False})

        return True

from odoo import models, _
from odoo.exceptions import UserError


class StockReturnPicking(models.TransientModel):
    _inherit = 'stock.return.picking'

    def _tradeline_get_next_request_transfers(self, picking):
        if not picking or not picking.request_id or picking.picking_type_code != 'internal':
            return self.env['stock.picking']
        return picking.request_id.transfer_ids.filtered(
            lambda transfer: transfer.id != picking.id
            and transfer.picking_type_code == 'internal'
            and transfer.location_id.id == picking.location_dest_id.id
        )

    def _tradeline_get_next_origin_transfers(self, picking):
        if not picking or picking.picking_type_code != 'internal':
            return self.env['stock.picking']
        candidates = self.env['stock.picking'].search(
            [
                ("id", "!=", picking.id),
                ("company_id", "=", picking.company_id.id),
                ("picking_type_code", "=", "internal"),
                ("location_id", "=", picking.location_dest_id.id),
                ("origin", "!=", False),
            ]
        )
        return candidates.filtered(
            lambda transfer: picking.name in [
                reference.strip() for reference in (transfer.origin or "").split(",") if reference.strip()
            ]
        )

    def _tradeline_get_next_transfers(self, picking):
        next_transfers = self._tradeline_get_next_request_transfers(picking)
        if next_transfers:
            return next_transfers
        return self._tradeline_get_next_origin_transfers(picking)

    def create_returns(self):
        self.ensure_one()
        picking = self.picking_id
        request = picking.request_id
        next_transfers = self.env['stock.picking']

        if picking.picking_type_code == 'internal':
            next_transfers = self._tradeline_get_next_transfers(picking)
            done_next_transfers = next_transfers.filtered(lambda transfer: transfer.state == 'done')
            if done_next_transfers:
                transfer_names = ", ".join(done_next_transfers.mapped('name'))
                raise UserError(
                    _("Cannot create return because next transfer is already done: %s") % transfer_names
                )

        result = super().create_returns()

        if next_transfers:
            pending_next_transfers = next_transfers.filtered(
                lambda transfer: transfer.state not in ('done', 'cancel')
            )
            if pending_next_transfers:
                pending_next_transfers.with_context(
                    tradeline_return_cancel_next=True
                ).action_cancel()
            if request and request.state != 'cancel':
                request.action_cancel()
            if request:
                request._tradeline_refresh_source_documents()

        return result

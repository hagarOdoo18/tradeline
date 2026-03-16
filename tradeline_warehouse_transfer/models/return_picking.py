from odoo import models, _
from odoo.exceptions import UserError


class StockReturnPicking(models.TransientModel):
    _inherit = 'stock.return.picking'

    def _tradeline_prepare_return_chain(self):
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
        return request, next_transfers

    def _tradeline_apply_return_chain(self, request, next_transfers):
        if not next_transfers:
            return
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

    def _tradeline_get_next_request_transfers(self, picking):
        if not picking or not picking.request_id or picking.picking_type_code != 'internal':
            return self.env['stock.picking']
        return picking.request_id.transfer_ids.filtered(
            lambda transfer: transfer.id != picking.id
            and transfer.picking_type_code == 'internal'
            and transfer.location_id.id == picking.location_dest_id.id
        )

    def _tradeline_get_next_move_transfers(self, picking):
        if not picking or picking.picking_type_code != 'internal':
            return self.env['stock.picking']
        return picking.move_ids_without_package.mapped('move_dest_ids.picking_id').filtered(
            lambda transfer: transfer.id != picking.id
            and transfer.picking_type_code == 'internal'
            and transfer.company_id.id == picking.company_id.id
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
        by_location = candidates.filtered(
            lambda transfer: picking.name in [
                reference.strip() for reference in (transfer.origin or "").split(",") if reference.strip()
            ]
        )
        if by_location:
            return by_location
        # Fallback for legacy chains where route/source location changed after transfer creation.
        fallback_candidates = self.env['stock.picking'].search(
            [
                ("id", "!=", picking.id),
                ("company_id", "=", picking.company_id.id),
                ("picking_type_code", "=", "internal"),
                ("origin", "!=", False),
            ]
        )
        return fallback_candidates.filtered(
            lambda transfer: picking.name in [
                reference.strip() for reference in (transfer.origin or "").split(",") if reference.strip()
            ]
        )

    def _tradeline_get_next_transfers(self, picking):
        next_transfers = self._tradeline_get_next_request_transfers(picking)
        next_transfers |= self._tradeline_get_next_move_transfers(picking)
        next_transfers |= self._tradeline_get_next_origin_transfers(picking)
        return next_transfers.filtered(lambda transfer: transfer.id != picking.id)

    def create_returns(self):
        if self.env.context.get('tradeline_return_hook_handled'):
            return super().create_returns()
        request, next_transfers = self._tradeline_prepare_return_chain()
        result = super(StockReturnPicking, self.with_context(
            tradeline_return_hook_handled=True
        )).create_returns()
        self._tradeline_apply_return_chain(request, next_transfers)
        return result

    def _create_returns(self):
        if self.env.context.get('tradeline_return_hook_handled'):
            return super()._create_returns()
        request, next_transfers = self._tradeline_prepare_return_chain()
        result = super(StockReturnPicking, self.with_context(
            tradeline_return_hook_handled=True
        ))._create_returns()
        self._tradeline_apply_return_chain(request, next_transfers)
        return result

    def _create_return(self):
        if self.env.context.get('tradeline_return_hook_handled'):
            return super()._create_return()
        request, next_transfers = self._tradeline_prepare_return_chain()
        result = super(StockReturnPicking, self.with_context(
            tradeline_return_hook_handled=True
        ))._create_return()
        self._tradeline_apply_return_chain(request, next_transfers)
        return result

    def action_create_returns(self):
        if self.env.context.get('tradeline_return_hook_handled'):
            return super().action_create_returns()
        request, next_transfers = self._tradeline_prepare_return_chain()
        result = super(StockReturnPicking, self.with_context(
            tradeline_return_hook_handled=True
        )).action_create_returns()
        self._tradeline_apply_return_chain(request, next_transfers)
        return result

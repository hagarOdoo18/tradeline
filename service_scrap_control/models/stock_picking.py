from odoo import _, api, fields, models
from odoo.exceptions import UserError

SERVICE_WAREHOUSE_CODES = ('SER-W', 'SW-XP')
SERVICE_SCRAP_OPERATION_NAME = 'Service Scrap Location'
SERVICE_VENDOR_OPERATION_NAME = 'Service Vendor Location'


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    request_scrap = fields.Boolean(copy=False, tracking=True)
    wait_approve_scrap = fields.Boolean(copy=False, tracking=True)
    approve_scrap = fields.Boolean(copy=False, tracking=True)
    check_scrap_new = fields.Boolean(compute='_compute_check_scrap_new')

    @api.depends('picking_type_code', 'picking_type_id', 'picking_type_id.warehouse_id')
    def _compute_check_scrap_new(self):
        can_in_out = self.env.user.has_group('service_scrap_control.group_picking_scrap')
        can_internal = self.env.user.has_group('service_scrap_control.group_picking_scrap_transfer')
        for picking in self:
            warehouse = picking.picking_type_id.warehouse_id
            if not warehouse or warehouse.code not in SERVICE_WAREHOUSE_CODES:
                picking.check_scrap_new = False
                continue
            if picking.picking_type_code in ('incoming', 'outgoing'):
                picking.check_scrap_new = can_in_out
            elif picking.picking_type_code == 'internal':
                picking.check_scrap_new = can_internal
            else:
                picking.check_scrap_new = False

    def _is_service_scrap_scope(self):
        self.ensure_one()
        warehouse = self.picking_type_id.warehouse_id
        return bool(
            warehouse
            and warehouse.code in SERVICE_WAREHOUSE_CODES
            and self.picking_type_code == 'internal'
        )

    def _notify_scrap_approvers(self, body):
        group = self.env.ref('service_scrap_control.group_approval_scrap', raise_if_not_found=False)
        partner_ids = group.users.mapped('partner_id').ids if group else []
        self.message_post(body=body, partner_ids=partner_ids)

    def action_open_request_scrap_wizard(self):
        self.ensure_one()
        action = self.env['ir.actions.actions']._for_xml_id(
            'service_scrap_control.action_request_scrap_wizard'
        )
        action['context'] = {
            'default_picking_id': self.id,
        }
        return action

    def action_request_scrap(self, note):
        self.ensure_one()
        if not self._is_service_scrap_scope():
            raise UserError(_('Request scrap is only available for Service internal transfers.'))

        vals = {
            'request_scrap': True,
            'wait_approve_scrap': True,
            'approve_scrap': False,
        }
        self.write(vals)

        body = _(
            'Scrap request submitted by %(user)s.<br/><b>Note:</b> %(note)s'
        ) % {
            'user': self.env.user.display_name,
            'note': note or '-',
        }
        self._notify_scrap_approvers(body)
        return True

    def function_approve_scrap(self):
        if not self.env.user.has_group('service_scrap_control.group_approval_scrap'):
            raise UserError(_('You do not have permission to approve scrap requests.'))

        for picking in self:
            if not picking.wait_approve_scrap:
                continue
            picking.write({
                'approve_scrap': True,
                'wait_approve_scrap': False,
                'request_scrap': True,
            })
            picking.message_post(
                body=_('Scrap request approved by %(user)s.')
                % {'user': self.env.user.display_name}
            )
        return True

    def _get_service_scrap_location(self, vendor=False):
        self.ensure_one()
        warehouse = self.picking_type_id.warehouse_id
        if not warehouse:
            return self.env['stock.location']

        operation_name = SERVICE_VENDOR_OPERATION_NAME if vendor else SERVICE_SCRAP_OPERATION_NAME
        operation_type = self.env['stock.picking.type'].search([
            ('warehouse_id', '=', warehouse.id),
            ('code', '=', 'internal'),
            ('name', '=', operation_name),
        ], limit=1)
        if operation_type and operation_type.default_location_dest_id:
            location = operation_type.default_location_dest_id
            if vendor and location.scrap_vendor_location:
                return location
            if not vendor and location.scrap_location:
                return location

        fallback_domain = [
            ('company_id', '=', self.company_id.id),
            ('scrap_location', '=', True),
        ]
        if vendor:
            fallback_domain.append(('scrap_vendor_location', '=', True))
        return self.env['stock.location'].search(fallback_domain, limit=1)

    def _open_service_scrap_wizard(self, vendor=False):
        self.ensure_one()
        return {
            'name': _('Vendor Scrap') if vendor else _('Request Scrap'),
            'type': 'ir.actions.act_window',
            'res_model': 'stock.scrap.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_picking_id': self.id,
                'default_vendor': vendor,
            },
        }

    def button_vendor(self):
        self.ensure_one()
        if not self._is_service_scrap_scope():
            raise UserError(_('Vendor scrap is only available for Service internal transfers.'))
        if not self.env.user.has_group('service_scrap_control.group_return_picking_internal'):
            raise UserError(_('You do not have permission to run vendor scrap.'))
        return self._open_service_scrap_wizard(vendor=True)

    def button_scrap(self):
        self.ensure_one()
        if self._is_service_scrap_scope():
            if not self.check_scrap_new:
                raise UserError(_('You do not have permission to scrap this transfer.'))
            if not self.approve_scrap:
                raise UserError(_('Scrap request must be approved before validating scrap.'))
            return self._open_service_scrap_wizard(vendor=False)
        return super().button_scrap()

from odoo import _, api, fields, models
from odoo.exceptions import UserError

SERVICE_SCRAP_LOCATION_NAMES = {
    'Service Scrapped',
    'Vendor Scrapped',
    'Service Scrapped XPRS',
    'Vendor Scrapped XPRS',
}


class StockScrap(models.Model):
    _inherit = 'stock.scrap'

    state = fields.Selection(
        selection_add=[
            ('witting', 'Waiting Approve'),
            ('approve', 'Approved'),
        ],
        ondelete={
            'witting': 'set default',
            'approve': 'set default',
        },
    )
    vendor_scrap = fields.Boolean(copy=False)
    service_flow_scope = fields.Boolean(compute='_compute_service_flow_scope')

    @api.depends('picking_id', 'scrap_location_id')
    def _compute_service_flow_scope(self):
        for scrap in self:
            by_picking = bool(scrap.picking_id and scrap.picking_id._is_service_scrap_scope())
            by_location = bool(
                scrap.scrap_location_id
                and scrap.scrap_location_id.name in SERVICE_SCRAP_LOCATION_NAMES
            )
            scrap.service_flow_scope = by_picking or by_location

    def _notify_scrap_approvers(self, body):
        group = self.env.ref('service_scrap_control.group_approval_scrap', raise_if_not_found=False)
        partner_ids = group.users.mapped('partner_id').ids if group else []
        self.message_post(body=body, partner_ids=partner_ids)

    def request_request(self):
        for scrap in self:
            if scrap.state != 'draft':
                continue
            scrap.write({'state': 'witting'})
            body = _('Scrap request submitted by %(user)s.') % {
                'user': self.env.user.display_name,
            }
            scrap._notify_scrap_approvers(body)
        return True

    def function_approve_scrap(self):
        if not self.env.user.has_group('service_scrap_control.group_approval_scrap'):
            raise UserError(_('You do not have permission to approve scrap requests.'))

        for scrap in self:
            if scrap.state not in ('draft', 'witting'):
                continue
            scrap.write({'state': 'approve'})
            scrap.message_post(
                body=_('Scrap approved by %(user)s.')
                % {'user': self.env.user.display_name}
            )
        return True

    def action_validate(self):
        blocked = self.filtered(
            lambda scrap: scrap.service_flow_scope
            and not scrap.vendor_scrap
            and scrap.state != 'approve'
        )
        if blocked:
            raise UserError(_('You must approve scrap before validation.'))
        return super().action_validate()

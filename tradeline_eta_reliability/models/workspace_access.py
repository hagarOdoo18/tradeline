"""The recovery workspace is separate from ordinary branch invoicing rights."""
from odoo import api, models, SUPERUSER_ID, _
from odoo.exceptions import AccessError, UserError
from odoo.osv import expression


class EtaWorkspaceUser(models.Model):
    _inherit = 'res.users'

    @api.model
    def _eta_has_workspace_access(self):
        return self.env.uid == SUPERUSER_ID or self.env.user.has_group('tradeline_eta_reliability.group_eta_workspace')

    @api.model
    def _eta_check_workspace_access(self):
        if not self._eta_has_workspace_access():
            raise AccessError(_('ETA Status is available to central accounting. Contact accounting for invoice recovery.'))


class EtaWorkspaceInvoiceAccess(models.Model):
    _inherit = 'account.move'

    @api.model
    def get_view(self, view_id=None, view_type='form', **options):
        views = [self.env.ref('tradeline_eta_reliability.' + name, raise_if_not_found=False) for name in
            ('eta_workspace_invoice_list', 'eta_workspace_invoice_search')]
        protected = [view.id for view in views if view]
        if view_id in protected:
            self.env['res.users']._eta_check_workspace_access()
        result = super().get_view(view_id=view_id, view_type=view_type, **options)
        if result.get('id') in protected:
            self.env['res.users']._eta_check_workspace_access()
        return result

    def action_eta_status_details(self):
        self.env['res.users']._eta_check_workspace_access()
        return super().action_eta_status_details()

    def action_eta_correct_vat(self):
        self.env['res.users']._eta_check_workspace_access()
        return super().action_eta_correct_vat()

    def action_eta_vat_documents(self):
        self.env['res.users']._eta_check_workspace_access()
        action = super().action_eta_vat_documents()
        if not self.env.context.get('eta_show_cancelled_corrections'):
            action['domain'] = expression.AND([action['domain'], [('state', '!=', 'cancel')]])
            action['name'] = _('Active VAT corrections: %s', self.name)
        return action

    def action_eta_vat_history(self):
        self.env['res.users']._eta_check_workspace_access()
        return self.with_context(eta_show_cancelled_corrections=True).action_eta_vat_documents()

    def action_eta_start_vat_correction(self):
        self.ensure_one()
        self.env['res.users']._eta_check_workspace_access()
        self.check_access('write')
        if self.eta_vat_workflow not in ('unsent', 'approved', 'rejected'):
            raise UserError(_('Open the existing corrections or review the ETA outcome before preparing VAT changes.'))
        if self.state == 'draft':
            return {'type': 'ir.actions.act_window', 'res_model': 'account.move',
                    'view_mode': 'form', 'res_id': self.id}
        return self.action_eta_correct_vat()


class EtaWorkspaceJobAccess(models.Model):
    _inherit = 'tradeline.eta.job'

    @api.model
    def get_view(self, view_id=None, view_type='form', **options):
        self.env['res.users']._eta_check_workspace_access()
        return super().get_view(view_id=view_id, view_type=view_type, **options)


class EtaWorkspaceActionAccess(models.Model):
    _inherit = 'ir.actions.actions'

    def _get_action_dict(self):
        self.ensure_one()
        owned = self.env['ir.model.data'].sudo().search_count([
            ('module', '=', 'tradeline_eta_reliability'),
            ('model', '=like', 'ir.actions.%'), ('res_id', '=', self.id)])
        if owned:
            # The web action loader may sudo metadata reads; use the actual uid
            # rather than env.su so a branch administrator is still excluded.
            self.env['res.users']._eta_check_workspace_access()
        return super()._get_action_dict()

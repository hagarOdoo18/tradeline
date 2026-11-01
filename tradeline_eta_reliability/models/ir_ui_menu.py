from odoo import api, models


class EtaStaffMenu(models.Model):
    _inherit = 'ir.ui.menu'

    @api.model
    def _eta_place_staff_menu(self):
        accounting = self.env.ref('accountant.menu_accounting', raise_if_not_found=False)
        menu = self.env.ref('tradeline_eta_reliability.menu_account_eta_jobs')
        group = self.env.ref('tradeline_eta_reliability.group_eta_workspace')
        own = self.env['ir.model.data'].sudo().search([
            ('module', '=', 'tradeline_eta_reliability'), ('model', '=', 'ir.ui.menu')])
        self.browse(own.mapped('res_id')).write({'groups_id': [(6, 0, group.ids)]})
        # Community-only installations have no separate Accounting application.
        # Never hide their only accounting entry point.
        if accounting:
            menu.write({'parent_id': accounting.id, 'name': 'ETA Status'})
            invoicing = self.env.ref('account.menu_finance')
            invoicing.write({'active': False})

    @api.model
    def _visible_menu_ids(self, debug=False):
        visible = super()._visible_menu_ids(debug=debug)
        if not self.env['res.users']._eta_has_workspace_access():
            own = self.env['ir.model.data'].sudo().search([
                ('module', '=', 'tradeline_eta_reliability'), ('model', '=', 'ir.ui.menu')])
            visible = visible.difference(own.mapped('res_id'))
        accounting = self.env.ref('accountant.menu_accounting', raise_if_not_found=False)
        invoicing = self.env.ref('account.menu_finance', raise_if_not_found=False)
        # Retain this requirement even if an unrelated module upgrade re-enables
        # the root. The accounting functions remain in the existing Accounting app.
        if accounting and invoicing:
            return visible.difference({invoicing.id})
        return visible

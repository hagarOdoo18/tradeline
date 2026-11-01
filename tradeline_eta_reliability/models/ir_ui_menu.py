from odoo import api, models


class EtaStaffMenu(models.Model):
    _inherit = 'ir.ui.menu'

    @api.model
    def _eta_place_staff_menu(self):
        accounting = self.env.ref('accountant.menu_accounting', raise_if_not_found=False)
        menu = self.env.ref('tradeline_eta_reliability.menu_account_eta_jobs')
        # Community-only installations have no separate Accounting application.
        # Never hide their only accounting entry point.
        if accounting:
            menu.write({'parent_id': accounting.id, 'name': 'ETA Status'})
            invoicing = self.env.ref('account.menu_finance')
            invoicing.write({'active': False})

    @api.model
    def _visible_menu_ids(self, debug=False):
        visible = super()._visible_menu_ids(debug=debug)
        accounting = self.env.ref('accountant.menu_accounting', raise_if_not_found=False)
        invoicing = self.env.ref('account.menu_finance', raise_if_not_found=False)
        # Retain this requirement even if an unrelated module upgrade re-enables
        # the root. The accounting functions remain in the existing Accounting app.
        if accounting and invoicing:
            return visible.difference({invoicing.id})
        return visible

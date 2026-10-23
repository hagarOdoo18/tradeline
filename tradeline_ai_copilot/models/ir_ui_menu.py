from odoo import api, models


class IrUiMenu(models.Model):
    _inherit = "ir.ui.menu"

    @api.model
    def _visible_menu_ids(self, debug=False):
        visible_ids = super()._visible_menu_ids(debug=debug)
        administrator = self.env.ref("base.user_admin", raise_if_not_found=False)
        root = self.env.ref("tradeline_ai_copilot.menu_tradeline_ai_root", raise_if_not_found=False)
        if not root or (administrator and self.env.uid == administrator.id):
            return visible_ids

        hidden_ids = self.sudo().with_context(**{"ir.ui.menu.full_list": True}).search([
            ("id", "child_of", root.id),
        ]).ids
        # Core caches by groups, so filter by account afterwards and copy its set.
        return visible_ids.difference(hidden_ids)

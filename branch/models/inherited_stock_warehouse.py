# Part of BrowseInfo. See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models, _
from odoo.exceptions import UserError

class StockWarehouse(models.Model):
    _inherit = 'stock.warehouse'

    branch_id = fields.Many2one('res.branch')

    default_transfer = fields.Boolean(
        string='Default Transfer',
        required=False)

    @api.model
    def default_get(self, default_fields):
        res = super(StockWarehouse, self).default_get(default_fields)
        branch_id = False
        if self._context.get('branch_id'):
            branch_id = self._context.get('branch_id')
        elif self.env.user.branch_id:
            branch_id = self.env.user.branch_id.id
        res.update({'branch_id': branch_id})
        return res

    # @api.onchange('branch_id')
    # def _onchange_branch_id(self):
    #     selected_brach = self.branch_id
    #     if selected_brach:
    #         user_id = self.env['res.users'].browse(self.env.uid)
    #         user_branch = user_id.sudo().branch_id
    #         if user_branch and user_branch.id != selected_brach.id:
    #             raise UserError("Please select active branch only. Other may create the Multi branch issue. \n\ne.g: If you wish to add other branch then Switch branch from the header and set that.")

    @api.model
    def search_fetch(self, domain, field_names, offset=0, limit=None, order=None):
        if self.env.context.get('form'):
            domain += ['|', ('branch_id', '=', False), ('branch_id', 'in', self.env.user.branch_ids.ids)]


        return super().search_fetch(domain, field_names, offset, limit, order)


class StockPickingTypeIn(models.Model):
    _inherit = 'stock.picking.type'

    branch_id = fields.Many2one('res.branch',related='warehouse_id.branch_id', store=True,)

    @api.model
    def search_fetch(self, domain, field_names, offset=0, limit=None, order=None):

        domain += ['|',('branch_id','=',False),('branch_id','in',self.env.user.branch_ids.ids)]

        return super().search_fetch(domain, field_names, offset, limit, order)

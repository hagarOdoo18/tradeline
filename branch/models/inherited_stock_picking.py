# Part of BrowseInfo. See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    @api.model
    def default_get(self, default_fields):
        res = super(StockPicking, self).default_get(default_fields)
        if self.env.user.branch_id:
            res.update({
                'branch_id' : self.env.user.branch_id.id or False
            })
        return res

    branch_id = fields.Many2one('res.branch', string="Branch")

    @api.model
    def search_fetch(self, domain, StockLocation, offset=0, limit=None, order=None):
        if  self.env.user.id  not in[1,2]:
            domain += ['|', ('branch_id', '=', False), ('branch_id', 'in', self.env.user.branch_ids.ids)]

        return super().search_fetch(domain, StockLocation, offset, limit, order)

    @api.model
    def set_branch(self):
        for rec in self:
            if rec.location_dest_id.warehouse_id.branch_id:
                rec.branch_id = rec.location_dest_id.warehouse_id.branch_id.id
            else:
                rec.branch_id = rec.location_id.warehouse_id.branch_id.id




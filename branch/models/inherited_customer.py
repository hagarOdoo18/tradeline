# Part of BrowseInfo. See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models, _

from odoo.exceptions import UserError

class ResPartnerIn(models.Model):
    _inherit = 'res.partner'

    @api.model_create_multi
    def create(self, values):
        # Add code here
        if self.env.user.id not in [1, 2]:
            if not self.env.user.has_group('branch.group_allow_create_partner') :
                raise UserError("Not Allowed For Create Customer")

        return super(ResPartnerIn, self).create(values)
    
    @api.model_create_multi
    def default_get(self, default_fields):
        res = super(ResPartnerIn, self).default_get(default_fields)
        if self.env.user.branch_id:
            res.update({
                'branch_id' : self.env.user.branch_id.id or False
            })
        return res

    branch_id = fields.Many2one('res.branch', string="Branch")
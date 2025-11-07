from odoo import fields, models, api


class Crmteam(models.Model):
    _inherit = 'crm.team'

    branch_id = fields.Many2one('res.branch', string="Branch",required=True)

class CrmLead(models.Model):
    _inherit = 'crm.lead'

    default_branch_id = fields.Many2one('res.branch', string="default Branch",required=True,readonly=True)
    branch_id = fields.Many2one('res.branch', string="Branch",required=True)
    sales_rep_id = fields.Many2one(
        comodel_name='sales.rep',
        string='Sales Rep',
        required=True)
    sales_rep_domain = fields.Char(
        string='Sales_rep_domain',
        required=False)
    @api.onchange('default_branch_id')
    def onchange_method(self):
        if self.default_branch_id:
            self.sales_rep_domain = "[('branch_id','='," + str(self.default_branch_id.id) + ")]"
        else:
            self.sales_rep_domain = "[('branch_id','=',False)]"

    @api.model_create_multi
    def default_get(self, fields):
        res = super(CrmLead, self).default_get(fields)
        branch_id = self.env.user.branch_id.id

        res.update({
            'default_branch_id': branch_id,
        })

        return res

    @api.onchange('branch_id')
    def onchange_branch_id(self):
        branched_warehouse = self.env['stock.warehouse'].search([('branch_id', '=', self.branch_id.id),('company_id','=',self.env.company.id)])
        team = self.env['crm.team'].search([('branch_id', '=', self.default_branch_id.id),('company_id','=',self.env.company.id)])
        self.user_id = self.branch_id.user_id.id
        if team:
            self.team_id = team.id
        if branched_warehouse:
            self.warehouse_id = branched_warehouse.ids[0]

class Lead2OpportunityPartner_inher(models.TransientModel):
    _inherit = 'crm.lead2opportunity.partner'
    _name = 'crm.lead2opportunity.partner'

    branch_id = fields.Many2one('res.branch', string="Branch",required=True)
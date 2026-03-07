# Part of BrowseInfo. See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models, _

class ResBranch(models.Model):
    _name = 'res.branch'
    _description = 'Branch'
    _order = 'name asc'

    name = fields.Char(required=True)
    company_id = fields.Many2one('res.company', required=True)
    telephone = fields.Char(string='Telephone No')
    apple_store_id = fields.Char(string='Apple Store Id')
    address = fields.Text('Address')
    sales_rep_ids = fields.One2many(
        'sales.rep','branch_id',
        string='Sales rep',
        required=False)
    user_id = fields.Many2one(
        comodel_name='res.users',
        string='Related Users',
        required=False)

    @api.model
    def _name_search(self, name, domain=None, operator='ilike', limit=100, order=None):
        args = domain or []
        if self._context.get('allowed_company_ids'):
            selected_company_ids = self.env['res.company'].browse(self._context.get('allowed_company_ids'))
            if selected_company_ids:
                branches_ids = self.env['res.branch'].search([('company_id','in',selected_company_ids.ids)])
                domain += [('id', 'in', branches_ids.ids)]
            return super(ResBranch, self)._name_search(name=name, domain=domain, operator=operator, limit=limit,
                                                       order=order)
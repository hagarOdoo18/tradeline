from odoo import fields, models, api


class DiscountReason(models.Model):
    _inherit = 'discount.reason'

    branches_ids = fields.Many2many(
        comodel_name='res.branch',
        string='branches',
        required=True)

class SalesRep(models.Model):
    _inherit = 'sales.rep'

    branch_id = fields.Many2one(
        comodel_name='res.branch',
        string='branch',
        required=False)



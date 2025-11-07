# Part of BrowseInfo. See LICENSE file for full copyright and licensing details.

from odoo import fields, models

from odoo.tools.sql import SQL

class PurchaseReport(models.Model):
    _inherit = "purchase.report"

    branch_id = fields.Many2one('res.branch')


    def _select(self) -> SQL:
        return SQL("%s, po.branch_id AS branch_id",
                   super()._select())
    def _group_by(self) -> SQL:
        return SQL("%s, po.branch_id",
                   super()._group_by())



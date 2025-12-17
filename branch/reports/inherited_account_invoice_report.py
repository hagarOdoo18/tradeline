# Part of BrowseInfo. See LICENSE file for full copyright and licensing details.

from odoo import fields, models,api
from odoo.tools.sql import SQL
class AccountInvoiceReport(models.Model):
    _inherit = "account.invoice.report"

    branch_id = fields.Many2one('res.branch')

    reference_number = fields.Char(
        string='Reference Number',
        required=False)
    preferred_payment_method_line_id = fields.Many2one(
        string="Payment Method",
        comodel_name='account.payment.method.line',
        store=True,
        readonly=False,
    )

    pricelist_id = fields.Many2one('product.pricelist', string='Pricelist')


def _select(self) -> SQL:
        return SQL("%s, move.branch_id AS branch_id, move.reference_number AS reference_number,move.preferred_payment_method_line_id as preferred_payment_method_line_id,move.pricelist_id as pricelist_id",
                   super()._select())


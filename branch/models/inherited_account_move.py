# Part of BrowseInfo. See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.tools.float_utils import float_compare
from dateutil.relativedelta import relativedelta
import calendar

from datetime import date
class AccountMove(models.Model):
    _inherit = 'account.move'

    @api.model
    def default_get(self, default_fields):
        res = super(AccountMove, self).default_get(default_fields)
        branch_id = False

        if self._context.get('branch_id'):
            branch_id = self._context.get('branch_id')
        elif self.env.user.branch_id:
            branch_id = self.env.user.branch_id.id
        res.update({
            'branch_id' : branch_id
        })
        return res

    branch_id = fields.Many2one('res.branch', string="Branch",readonly=True)

    # @api.onchange('branch_id')
    # def _onchange_branch_id(self):
    #     selected_brach = self.branch_id
    #     if selected_brach:
    #         user_id = self.env['res.users'].browse(self.env.uid)
    #         user_branch = user_id.sudo().branch_id
    #         if user_branch and user_branch.id != selected_brach.id:
    #             raise UserError("Please select active branch only. Other may create the Multi branch issue. \n\ne.g: If you wish to add other branch then Switch branch from the header and set that.")


    #
    # def _get_starting_sequence(self):
    #     # EXTENDS account sequence.mixin
    #     self.ensure_one()
    #     move_date = self.date or self.invoice_date or fields.Date.context_today(self)
    #     year_part = "%04d" % move_date.year
    #     last_day = int(self.company_id.fiscalyear_last_day)
    #     last_month = int(self.company_id.fiscalyear_last_month)
    #     is_staggered_year = last_month != 12 or last_day != 31
    #     if is_staggered_year:
    #         max_last_day = calendar.monthrange(move_date.year, last_month)[1]
    #         last_day = min(last_day, max_last_day)
    #         if move_date > date(move_date.year, last_month, last_day):
    #             year_part = "%s-%s" % (move_date.strftime('%y'), (move_date + relativedelta(years=1)).strftime('%y'))
    #         else:
    #             year_part = "%s-%s" % ((move_date + relativedelta(years=-1)).strftime('%y'), move_date.strftime('%y'))
    #     # Arbitrarily use annual sequence for sales documents, but monthly
    #     # sequence for other documents
    #     if self.journal_id.type in ['sale', 'bank', 'cash', 'credit']:
    #         # We reduce short code to 4 characters (0000) in case of staggered
    #         # year to avoid too long sequences (see Indian GST rule 46(b) for
    #         # example). Note that it's already the case for monthly sequences.
    #         starting_sequence = "%s/%s/%s" % (self.journal_id.code, year_part, '0000' if is_staggered_year else '00000')
    #     else:
    #         starting_sequence = "%s/%s/%02d/0000" % (self.journal_id.code, year_part, move_date.month)
    #     if self.journal_id.refund_sequence and self.move_type in ('out_refund', 'in_refund'):
    #         starting_sequence = "RI/" + starting_sequence
    #     if self.journal_id.payment_sequence and self.origin_payment_id or self.env.context.get('is_payment'):
    #         starting_sequence = "P/" + starting_sequence
    #     return starting_sequence


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'



    branch_id = fields.Many2one('res.branch', string="Branch")

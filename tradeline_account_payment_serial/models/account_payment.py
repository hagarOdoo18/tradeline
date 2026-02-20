from odoo import fields, models, api, _
from odoo.exceptions import UserError

import logging
_logger = logging.getLogger(__name__)


class AccountPayment(models.Model):
    _inherit = 'account.payment'

    is_voucher = fields.Boolean(string='Is Voucher')
    is_admin_serial = fields.Boolean(
        string='Is Admin Serial',
        compute='_compute_is_admin_serial',
    )

    lot_id = fields.Many2one(
        'stock.lot', 'Voucher Serial',
        domain="[('product_id.categ_id', '=', 55)]",
    )

    def _compute_is_admin_serial(self):
        is_admin = self.env.user.has_group(
            'tradeline_account_payment_serial.group_Admin_payment_serial'
        )
        for rec in self:
            rec.is_admin_serial = is_admin

    @api.constrains('lot_id')
    def _check_serial(self):
        for rec in self:
            if not rec.lot_id:
                continue
            payments = self.search([
                ('lot_id', '=', rec.lot_id.id),
                ('payment_type', '=', rec.payment_type),
                ('id', '!=', rec.id),
            ])
            if rec.lot_id.expiration_date and rec.lot_id.expiration_date < fields.Datetime.now():
                raise UserError(_('Voucher serial is expired'))
            if payments:
                raise UserError(_('This Serial IS Used Before !!'))
            max_amount = round(rec.lot_id.product_id.product_tmpl_id.price_with_taxes)
            if rec.amount > max_amount:
                raise UserError(
                    _('Amount Must Be less Than Or equal %s', max_amount)
                )
            if rec.lot_id.product_qty != 0:
                raise UserError(_('Please OUT This Voucher Serial From Stock First'))

    @api.onchange('lot_id')
    def _check_serial_change(self):
        if self.lot_id:
            payments = self.search([
                ('lot_id', '=', self.lot_id.id),
                ('payment_type', '=', self.payment_type),
                ('id', '!=', self._origin.id),
            ])
            if self.lot_id.expiration_date and self.lot_id.expiration_date < fields.Datetime.now():
                raise UserError(_('Voucher serial is expired'))
            if payments:
                raise UserError(_('This Serial IS Used Before !!'))
            max_amount = round(self.lot_id.product_id.lst_price)
            if self.amount > max_amount:
                raise UserError(
                    _('Amount Must Be less Than Or equal %s', max_amount)
                )
            if self.lot_id.product_qty != 0:
                raise UserError(_('Please OUT This Voucher Serial From Stock First'))

    @api.onchange('journal_id')
    def _onchange_journal_id_voucher(self):
        for rec in self:
            # NOTE: Hardcoded journal ID 751 from original module.
            # Consider using a configurable setting or journal field instead.
            rec.is_voucher = bool(rec.journal_id.id in [ 528,580])

    @api.onchange('amount')
    def _onchange_amount_voucher(self):
        for rec in self:
            if rec.lot_id and rec.journal_id.id in [ 528,580]:
                max_amount = rec.lot_id.product_id.lst_price
                if rec.amount > max_amount:
                    raise UserError(
                        _('Amount Must Be less Than Or equal %s', max_amount)
                    )

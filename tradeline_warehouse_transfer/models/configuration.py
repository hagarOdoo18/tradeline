# -*- coding: utf-8 -*-

from odoo import models, fields, api,_
from odoo.exceptions import UserError

class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'


    warehouse_ids = fields.Many2many(
        comodel_name='stock.warehouse',
        related='company_id.warehouse_ids',readonly=False,
        string='Warehouses')

    stock_journal = fields.Many2one(
        comodel_name='account.journal',domain=[('type','=','general')],
        related='company_id.stock_journal',readonly=False,
        string='Stock Journal',
        required=False)

    debit_account = fields.Many2one(
        comodel_name='account.account',readonly=False,
        related='company_id.debit_account',
        string='Debit Account',
        required=False)

    credit_account = fields.Many2one(
        comodel_name='account.account',readonly=False,
        string='Credit Account',
        related='company_id.credit_account',
        required=False)


class ResCompany(models.Model):
    _inherit = 'res.company'

    warehouse_ids = fields.Many2many(
        comodel_name='stock.warehouse',
        string='Warehouses')

    stock_journal = fields.Many2one(
        comodel_name='account.journal', domain=[('type', '=', 'general')],
        string='Stock Journal',
        required=False)

    debit_account = fields.Many2one(
        comodel_name='account.account',
        string='Debit Account',
        required=False)

    credit_account = fields.Many2one(
        comodel_name='account.account',
        string='Credit Account',
        required=False)
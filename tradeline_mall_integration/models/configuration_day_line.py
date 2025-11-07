from odoo import fields, models, api


class ConfigurationDayLine(models.Model):
    _name = 'config.day.line'

    date = fields.Date(
        string='Date',
        required=True,)
    branch_id = fields.Many2one('res.branch', 'branch', index=True,required=True)

    invoices_number = fields.Char(string="Invoice Number",readonly=False)
    untaxed_amount= fields.Float(
        string='Untaxed Amount', 
        readonly=True)
    total_amount= fields.Float(
        string='Total Amount', 
        readonly=True)
    tax_amount= fields.Float(
        string='tax Amount',
        readonly=True)

    new_untaxed_amount= fields.Float(
        string='New Untaxed Amount', 
        required=False)

    new_total_amount = fields.Float(
        string='New Total Amount',
        required=False)

    new_tax_amount = fields.Float(
        string='New tax Amount',
        required=False)

    confirm = fields.Boolean(
        string='Confirm',
        required=True)

    config_day_id = fields.Many2one(
        comodel_name='config.day',
        string='Config_day_id',
        required=False)


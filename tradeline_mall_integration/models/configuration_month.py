from odoo import fields, models, api
from datetime import date, timedelta, datetime
def get_years():
    year_list = []
    for i in range(2019, 2100):
        year_list.append((str(i), str(i)))
    return year_list

class ConfigurationMonth(models.Model):
    _name = 'config.month'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    month_selection = fields.Selection([('1', 'January'), ('2', 'February'), ('3', 'March'), ('4', 'April'),
                                                  ('5', 'May'), ('6', 'June'), ('7', 'July'), ('8', 'August'),
                                                  ('9', 'September'), ('10', 'October'), ('11', 'November'),
                                                  ('12', 'December'), ],
                                                 string='Month', default=str(datetime.now().month))

    year = fields.Selection(get_years(), string='Year', default=str(datetime.now().year),readonly=True)
    percentage = fields.Float(
        string='Percentage', track_visibility='onchange',
        required=False)
    is_created = fields.Boolean(
        string='Is_created',
        readonly=False)
    total_month = fields.Float(
        string='Total month',track_visibility='onchange',
        required=True)
    total_day = fields.Float(
        string='Total day',track_visibility='onchange',
        required=True,readonly=True,compute="_onchange_month_selection_and_total_month")
    min_invoice_amount = fields.Float(
        string='Min Invoice Amount',track_visibility='onchange',
        required=True)
    branch_id = fields.Many2one('res.branch', 'branch', index=True, required=True)
    send_per_invoice = fields.Boolean(
        string='Send per invoice',
        required=False)
    @api.onchange('total_month','month_selection')
    @api.depends('total_month','month_selection')
    def _onchange_month_selection_and_total_month (self):
        if self.total_month and int(self.month_selection):
            if int(self.month_selection) + 1 > 12 :
                number_days = (date (int(self.year)+1, 1, 1) - date (int(self.year), int(self.month_selection),
                                                                                     1)).days
            else:
                number_days = (date(int(self.year), int(self.month_selection) + 1, 1) - date(int(self.year), int(self.month_selection), 1)).days
            self.total_day = self.total_month / number_days
        else:
            self.total_day = 0




    @api.model
    def cron_create_config_day(self):
        config_months = self.env['config.month'].search([('is_created','=',False)])

        for config_month in config_months:
            if not config_month.send_per_invoice:
                if int(self.month_selection) + 1 > 12 :
                    number_days = (date (int(config_month.year)+1, 1, 1) - date (int(config_month.year), int(config_month.month_selection),
                                                                                         1)).days
                else:
                    number_days = (date(int(config_month.year), int(config_month.month_selection) + 1, 1) - date(int(config_month.year), int(config_month.month_selection), 1)).days
                d1 = date(int(config_month.year), int(config_month.month_selection), 1)
                d2 = date(int(config_month.year), int(config_month.month_selection), number_days)
                delta = d2 - d1
                days = [(d1 + timedelta(days=i)) for i in range(delta.days + 1)]
                config_month.is_created =True
                for day in days:
                    config_day = self.env['config.day'].create({
                        'date' : day,
                        'total_day': config_month.total_day,
                        'min_invoice_amount' : config_month.min_invoice_amount,
                        'branch_id' : config_month.branch_id.id,
                        'month_id' : config_month.id,
                    })
                    config_day.server_action_create_daily_table()


    
    def server_action_create_config_day(self):
        for config_month in self:
            if not config_month.send_per_invoice:
                if int(self.month_selection) + 1 > 12 :
                    number_days = (date (int(config_month.year) + 1, 1, 1) - date (int(config_month.year), int(config_month.month_selection),
                                                                      1)).days
                else :
                    number_days = (date (int(config_month.year), int(config_month.month_selection) + 1, 1) - date (int(config_month.year), int(config_month.month_selection),
                                                                                         1)).days
                d1 = date(int(config_month.year), int(config_month.month_selection), 1)
                d2 = date(int(config_month.year), int(config_month.month_selection), number_days)
                delta = d2 - d1
                days = [(d1 + timedelta(days=i)) for i in range(delta.days + 1)]
                config_month.is_created =True
                for day in days:
                    has_day= self.env['config.day'].search([('date','=',day),('branch_id','=',config_month.branch_id.id)])
                    if has_day :
                        has_day.write({
                            'date': day,
                            'total_day': config_month.total_day,
                            'min_invoice_amount': config_month.min_invoice_amount,
                            'branch_id': config_month.branch_id.id,
                            'month_id' : config_month.id,

                        })
                        has_day.server_action_create_daily_table()
                    else:
                        config_day = self.env['config.day'].create({
                            'date' : day,
                            'total_day': config_month.total_day,
                            'min_invoice_amount' : config_month.min_invoice_amount,
                            'branch_id' : config_month.branch_id.id,
                            'month_id': config_month.id,

                        })
                        config_day.server_action_create_daily_table()


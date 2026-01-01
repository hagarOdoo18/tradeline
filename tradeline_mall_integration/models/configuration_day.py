from odoo import fields, models, api ,exceptions
from datetime import datetime
from datetime import timedelta

import json
import logging
_logger = logging.getLogger(__name__)
from odoo.exceptions import ValidationError, UserError

import requests
class ConfigurationDay(models.Model):
    _name = 'config.day'

    x_css = fields.Html(
        string='CSS',
        sanitize=False,
        compute='_compute_css',
        store=False,
    )
    state = fields.Selection(
        string='State',
        selection=[('draft', 'Draft'),
                   ('done', 'Done'), ],
        default='draft' )

    @api.depends('state')
    def _compute_css(self):
        for application in self:
            # Modify below condition
            if application.state == 'done':
                # application.x_css = '<style>.o_form_button_edit {display: none !important;}</style>'
                application.x_css = False
            else:
                application.x_css = False
    def action_confirm_day(self):
        self.state = 'done'

    
    def action_confirm_all_liens(self):
        for line in self.config_day_lines:
            line.confirm=True

    name = fields.Char(
        string='Name', 
        required=False)

    date = fields.Date(
        string='Date',
        required=True,default=datetime.today().date() )
    total_day = fields.Float(
        string='Total Day', 
        required=True)

    old_total_amount = fields.Float(
        string='old total Day',
        readonly=True)

    new_total_day = fields.Float(
        string='New Total Day',
        readonly=True)
    new_tax_day = fields.Float(
        string='New taxes Day',
        readonly=True)
    new_untaxed_total_day = fields.Float(
        string='New Total Untaxed Amount Day',
        readonly=True)
    old_tax_day = fields.Float(
        string='Old taxes Day',
        readonly=True)
    old_untaxed_total_day = fields.Float(
        string='Old Total Untaxed Amount Day',
        readonly=True)
    min_invoice_amount = fields.Float(
        string='Min Invoice Amount',
        required=True)
    branch_id = fields.Many2one('res.branch', 'branch', index=True,required=True)
    is_post = fields.Boolean(
        string='Is_post',
        default=False)

    config_day_lines = fields.One2many(
        comodel_name='config.day.line',
        inverse_name='config_day_id',
        string='Config day lines',
        required=False)

    @api.model
    def _cron_post_new_invoices(self):
        if self._cr.dbname == "live_11nov_2024":
            get_day = 1
            while get_day <=1:
                days_to_send = self.env['config.day'].search([('is_post','=',False),('date','=',fields.Date.today() - timedelta(days=int(get_day))),('branch_id','!=',29)])

                if days_to_send:


                    url = "https://api.tradelinestores.net/StoresMallsintegrations/GetTransactions"
                    headers={"Content-Type": "application/json"}
                    store_list_id=[]
                    store_list_name=[]
                    for day in days_to_send:


                        if day.branch_id.id not in store_list_id:
                            store_list_id.append(day.branch_id.id)
                            store_list_name.append(day.branch_id.name)

                    for store_id in store_list_id:
                        # days_of_store = self.env['config.day'].search([('state', '=', 'done'),('date','=',fields.Date.today() -  timedelta(days=1)), ('is_post', '=', False),('branch_id','=',store_id)])
                        days_of_store = self.env['config.day'].search([('date','=',fields.Date.today() -  timedelta(days=int(get_day))), ('is_post', '=', False),('branch_id','=',store_id)])
                        if days_of_store:
                            days_line_of_store = self.env['config.day.line'].search([('config_day_id', '=', days_of_store.id)])
                            mall_integration = self.env['base.integration'].search([('branch', '=', store_id)])
                            if mall_integration:
                                # pass
                                 invoices= mall_integration._prepare_invoices(days_line_of_store)
                                 # mall_integration.post_store(invoices)
                                 days_of_store.is_post = True
                                 days_of_store.state = 'done'
                            else:
                                mallsTransactionsItems = self._prepare_invoices_day_line(days_line_of_store)
                                new_dict = {'StoreId':store_id,
                                            'StoreName':store_list_name[store_list_id.index(store_id)],
                                            'mallsTransactionsItems':mallsTransactionsItems}
                                pload = json.dumps(new_dict)
                                try:
                                    r = requests.post(url=url, data=pload, headers=headers,verify=False)
                                    days_of_store.is_post = True
                                    days_of_store.state='done'
                                    _logger.info(r)

                                except Exception as e:
                                    _logger.info("error at Post Api")
                                    _logger.info(e)


                    get_day += 1
                else:
                    _logger.info("No Data For Post")
                    get_day += 1

    def _prepare_invoices_day_line(self,days_line_of_store):
        mallsTransactionsItems_list =[]
        for line in days_line_of_store:
            mallsTransactionsItems_dict = {}
            mallsTransactionsItems_dict.setdefault('Transaction_No', line.invoices_number)
            mallsTransactionsItems_dict.setdefault('Transaction_Date', line.config_day_id.date.strftime('%Y-%m-%d'))
            mallsTransactionsItems_dict.setdefault('Transaction_Gross',round(line.new_untaxed_amount,2))
            mallsTransactionsItems_dict.setdefault('Transaction_Net', round(line.new_total_amount,2))
            mallsTransactionsItems_dict.setdefault('Taxes', round(line.new_tax_amount,2))
            mallsTransactionsItems_dict.setdefault('storeid', line.config_day_id.branch_id.id)
            mallsTransactionsItems_list.append(mallsTransactionsItems_dict)
        return mallsTransactionsItems_list

    def _prepare_invoices_day(self,day):
        transaction_data= {}
        StoreId = day.branch_id.id
        StoreName = day.branch_id.name
        transaction_data.setdefault('StoreId',StoreId)
        transaction_data.setdefault('StoreName',StoreName)
        mallsTransactionsItems = self._prepare_invoices_day_line(day)
        transaction_data.setdefault('mallsTransactionsItems',mallsTransactionsItems)
        return transaction_data

   
    def set_default(self):
        for day in self:
            for line in day.config_day_lines:
                if line.confirm == False:
                    line.new_untaxed_amount = line.untaxed_amount
                    line.new_tax_amount = line.tax_amount
                    line.new_total_amount = line.total_amount
                    line.confirm = True

    @api.model_create_multi
    def create(self, vals):
        for v in vals:
            v['name'] = self.env['ir.sequence'].next_by_code(
                'config.day')
        return super().create(vals)

    
    def unconfirm_all(self):
        for line in self.config_day_lines:
            line.confirm =False
        self.set_default()

   
    def unlink(self):
        for day in self:
            if day.state == 'draft':
                for line in day.config_day_lines:
                    line.unlink()
            else:
                raise exceptions.ValidationError("Can't delete Confirmed Day")

        return super(ConfigurationDay, self).unlink()

    def create_day_lines(self,new_invoices,day):
        invoice_list= []
        old_untaxed_amount =0
        old_total_amount =0
        old_tax_amount =0
        for invoice_key in dict(new_invoices).keys():
            line = (0, 0,
                    {
                        'invoices_number': invoice_key,
                        'date':new_invoices[invoice_key]['date'],
                        'branch_id':new_invoices[invoice_key]['branch_id'],
                        'untaxed_amount': new_invoices[invoice_key]['untaxed_amount'],
                        'total_amount': new_invoices[invoice_key]['total_amount'],
                        'tax_amount': new_invoices[invoice_key]['tax_amount'],
                        'new_untaxed_amount': new_invoices[invoice_key]['new_untaxed_amount'],
                        'new_total_amount': new_invoices[invoice_key]['new_total_amount'],
                        'new_tax_amount': new_invoices[invoice_key]['new_tax_amount'],
                        'config_day_id': day.id,
                        'confirm': True,
                    })
            old_untaxed_amount += new_invoices[invoice_key]['untaxed_amount']
            old_total_amount += new_invoices[invoice_key]['total_amount']
            old_tax_amount += new_invoices[invoice_key]['tax_amount']
            invoice_list.append(line)
            day.new_untaxed_total_day += new_invoices[invoice_key]['new_untaxed_amount']
            day.old_untaxed_total_day += new_invoices[invoice_key]['untaxed_amount']
            day.new_tax_day += new_invoices[invoice_key]['new_tax_amount']
            day.old_tax_day += new_invoices[invoice_key]['tax_amount']
            day.old_total_amount += new_invoices[invoice_key]['total_amount']
            day.new_total_day += new_invoices[invoice_key]['new_total_amount']
        for line in day.config_day_lines:
            line.unlink()

        day.write({'config_day_lines': [(5, 0, 0)]})
        day.write({'config_day_lines': invoice_list,'old_untaxed_total_day':old_untaxed_amount,'old_tax_day':old_tax_amount,'old_total_amount':old_total_amount})

    def check_table(self,day):
        total_new_invoices = 0
        total_new_tax = 0
        count_of_min_invoice_amount = 0
        for line in day.config_day_lines:
            total_new_invoices += line.new_total_amount
            total_new_tax += line.new_tax_amount
            if line.new_total_amount > day.min_invoice_amount:
                count_of_min_invoice_amount += 1
        if day.total_day < total_new_invoices  :
            new_percentage = ((100 * day.total_day) / total_new_invoices)
            if new_percentage < 99:
                day.new_total_day = total_new_invoices
                day.new_tax_day = total_new_tax
                day.new_untaxed_total_day = total_new_invoices - total_new_tax


                return new_percentage
            else:
                day.new_total_day = total_new_invoices
                day.new_tax_day = total_new_tax
                day.new_untaxed_total_day = total_new_invoices - total_new_tax

            return False

        else:
            day.new_total_day = total_new_invoices
            day.new_tax_day = total_new_tax
            day.new_untaxed_total_day = total_new_invoices - total_new_tax

            return False

    def recalculate_table(self,day,new_percentage):
        check = False
        for invoice in day.config_day_lines:
            if invoice.new_total_amount > day.min_invoice_amount  :
                new_total_amount = ((new_percentage * invoice.new_total_amount) / 100)
                new_tax_amount = ((new_percentage * invoice.new_tax_amount) / 100)
                new_untaxed_amount = new_total_amount - new_tax_amount
                invoice.new_untaxed_amount = new_untaxed_amount
                invoice.new_tax_amount = new_tax_amount
                invoice.new_total_amount = new_total_amount
                check =True
        return check

   
    @api.onchange('config_day_lines')
    def onchange_method(self):
        for day in self:
            total_amount = 0
            total_untaxed_amount = 0
            total_tax = 0
            for rec in day.config_day_lines:
                total_amount += rec.new_total_amount
                total_tax += rec.new_tax_amount
                total_untaxed_amount += rec.new_untaxed_amount
            day.new_total_day = total_amount
            day.new_tax_day = total_tax
            day.new_untaxed_total_day = total_untaxed_amount
            self._cr.commit()

    @api.model
    def _cron_create_daily_table(self):
        days_to_calculate = self.env['config.day'].search([('state','!=','done'),('date','=',fields.Date.today()-timedelta(days=1)),('branch_id','!=',29)])
        for day in days_to_calculate:
            if not day.config_day_lines:
                all_invoices = self.get_all_invoice_by_date_and_branch(day)
                if all_invoices:
                    new_invoices = self.calculate_new_amount(all_invoices,day)
                    self.create_day_lines(new_invoices,day)
                    self._cr.commit()
            percentage = True
            while percentage:
                percentage = self.check_table(day)
                if percentage:
                    if not self.recalculate_table(day,percentage):
                        self._cr.commit()
                        break

    @api.model
    def check_madinaty_invoice(self):
        invoices = self.env['config.day.line'].search([('config_day_id','=',False),('branch_id','=',29)])
        for inv in invoices:
            inv.unlink()

    @api.model
    def server_action_create_daily_table(self):
        for day in self:
            # if not day.config_day_lines:
            if day.branch_id.id !=29:
                all_invoices = self.get_all_invoice_by_date_and_branch(day)
                if all_invoices:
                    new_invoices = self.calculate_new_amount(all_invoices,day)
                    self.create_day_lines(new_invoices,day)
                    self._cr.commit()
                percentage = True
                while percentage:
                    percentage = self.check_table(day)
                    if percentage:
                        if percentage:
                            if not self.recalculate_table(day, percentage):
                                self._cr.commit()
                                break

    @api.model
    def action_update_madinty(self):
        for day in self:
            if day.branch_id.id ==29:
                untaxed_amount = 0
                tax_amount = 0
                total_amount = 0
                for line in day.config_day_lines:
                    line.new_untaxed_amount = line.untaxed_amount
                    line.new_total_amount = line.total_amount
                    line.new_tax_amount = line.tax_amount
                    untaxed_amount += line.untaxed_amount
                    tax_amount += line.tax_amount
                    total_amount += line.total_amount
                day.sudo(2).new_untaxed_total_day = day.sudo(2).old_untaxed_total_day = untaxed_amount
                day.sudo(2).new_tax_day = day.sudo(2).old_tax_day = tax_amount
                day.sudo(2).old_total_amount = day.sudo(2).new_total_day = total_amount
            else:
                raise ValidationError(
                    ("Not Allowed"))

    def get_all_invoice_by_date_and_branch(self,day):
        all_invoices = self.env['account.move'].search([('invoice_date','=',day.date),('branch_id','=',day.branch_id.id),('payment_state','in',['paid','in_payment','partial'])])
        if all_invoices:
            return all_invoices
        else:
            return False



    def get_total_invoices(self,invoices):
        total_invoices = 0
        for invoice in invoices:
            total_invoices += invoice.amount_total
        return  total_invoices


    def calculate_new_amount(self,invoices,day):
        expected_total_invoices = day.total_day
        invoice_dic = {}
        min_invoice_amount = day.min_invoice_amount
        total_invoices = day.get_total_invoices(invoices)
        # day.last_total_day = total_invoices
        for invoice in invoices:
                if total_invoices > expected_total_invoices:
                    division_percentage = ((100 * expected_total_invoices) / total_invoices)

                    if invoice.amount_total > min_invoice_amount:
                        new_total_amount = ((division_percentage * invoice.amount_total) / 100)
                        new_tax_amount = ((division_percentage * invoice.amount_tax) / 100)
                        new_untaxed_amount = new_total_amount - new_tax_amount
                        if invoice.name not in invoice_dic.keys():
                            if invoice.type != 'out_refund':
                                invoice_dic.setdefault(invoice.name,{'untaxed_amount':invoice.amount_untaxed,
                                                                       'total_amount':invoice.amount_total,
                                                                       'tax_amount':invoice.amount_tax,
                                                                       'new_untaxed_amount':new_untaxed_amount,
                                                                       'new_tax_amount':new_tax_amount,
                                                                       'new_total_amount':new_total_amount,
                                                                       'date':invoice.invoice_date,
                                                                        'branch_id': invoice.branch_id.id,})
                            else:
                                invoice_dic.setdefault(invoice.name, {'untaxed_amount': -1*invoice.amount_untaxed,
                                                                        'total_amount': -1*invoice.amount_total,
                                                                        'tax_amount': -1*invoice.amount_tax,
                                                                        'new_untaxed_amount': -1*new_untaxed_amount,
                                                                        'new_tax_amount': -1*new_tax_amount,
                                                                        'new_total_amount': -1*new_total_amount,
                                                                        'date': invoice.invoice_date,
                                                                        'branch_id': invoice.branch_id.id, })

                    else:
                        if invoice.number not in invoice_dic.keys():
                            if invoice.type != 'out_refund':
                                invoice_dic.setdefault(invoice.name,{'untaxed_amount':invoice.amount_untaxed,
                                                                       'total_amount':invoice.amount_total,
                                                                       'tax_amount':invoice.amount_tax,
                                                                       'new_untaxed_amount':invoice.amount_untaxed,
                                                                       'new_tax_amount':invoice.amount_tax,
                                                                       'new_total_amount':invoice.amount_total,
                                                                       'date': invoice.invoice_date,
                                                                       'branch_id': invoice.branch_id.id,
                                                                       })
                            else:
                                invoice_dic.setdefault(invoice.name, {'untaxed_amount': -1*invoice.amount_untaxed,
                                                                        'total_amount': -1*invoice.amount_total,
                                                                        'tax_amount': -1*invoice.amount_tax,
                                                                        'new_untaxed_amount': -1*invoice.amount_untaxed,
                                                                        'new_tax_amount': -1*invoice.amount_tax,
                                                                        'new_total_amount': -1*invoice.amount_total,
                                                                        'date': invoice.invoice_date,
                                                                        'branch_id': invoice.branch_id.id,
                                                                        })

                else:
                    if invoice.name not in invoice_dic.keys():

                        if invoice.type != 'out_refund':
                            invoice_dic.setdefault(invoice.name, {'untaxed_amount': invoice.amount_untaxed,
                                                                    'total_amount': invoice.amount_total,
                                                                    'tax_amount': invoice.amount_tax,
                                                                    'new_untaxed_amount': invoice.amount_untaxed,
                                                                    'new_tax_amount': invoice.amount_tax,
                                                                    'new_total_amount': invoice.amount_total,
                                                                    'date': invoice.invoice_date,
                                                                    'branch_id': invoice.branch_id.id,
                                                                    })
                        else:
                            invoice_dic.setdefault(invoice.name, {'untaxed_amount': -1 * invoice.amount_untaxed,
                                                                    'total_amount': -1 * invoice.amount_total,
                                                                    'tax_amount': -1 * invoice.amount_tax,
                                                                    'new_untaxed_amount': -1 * invoice.amount_untaxed,
                                                                    'new_tax_amount': -1 * invoice.amount_tax,
                                                                    'new_total_amount': -1 * invoice.amount_total,
                                                                    'date': invoice.invoice_date,
                                                                    'branch_id': invoice.branch_id.id,
                                                                    })

        #     if invoice.number not in invoice_dic.keys():
            #         invoice_dic.setdefault(invoice.number, {'untaxed_amount': -invoice.amount_untaxed,
            #                                                 'total_amount': -invoice.amount_total,
            #                                                 'tax_amount': -invoice.amount_tax,
            #                                                 'new_untaxed_amount': -invoice.amount_untaxed,
            #                                                 'new_tax_amount': -invoice.amount_tax,
            #                                                 'new_total_amount': -invoice.amount_total,
            #                                                 'date': invoice.invoice_date,
            #                                                 'branch_id': invoice.branch_id.id,
            #                                                 })



        return invoice_dic
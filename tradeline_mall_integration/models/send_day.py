from odoo import fields, models, api ,exceptions,_
import json
import logging
_logger = logging.getLogger(__name__)
import requests
import openpyxl
from tempfile import TemporaryFile
from odoo.exceptions import ValidationError, UserError
import xlsxwriter
from io import BytesIO
import base64
import logging
class send_day (models.Model) :
    _name = 'send.day'

    date = fields.Date(
        string='Date',
        required=False)


    file = fields.Binary(string="File",  )

    def import_excel ( self ) :
        # Generating of the excel file to be read by openpyxl
        if self.file:
            file = base64.decodestring (self.file)
            excel_fileobj = TemporaryFile ('wb+')
            excel_fileobj.write (file)
            excel_fileobj.seek (0)

            # Create workbook
            workbook = openpyxl.load_workbook (excel_fileobj, data_only=True)
            # Get the first sheet of excel file
            sheet = workbook [workbook.get_sheet_names () [0]]
            index = 0
            items_serials = []
            for row in sheet.rows :
                if index == 0 :
                    index += 1
                    continue
                index += 1
                invoice_number = row [0].value
                olds=self.env['config.day.line'].search([('invoices_number','=',invoice_number)])
                for old in olds:
                    old.unlink()
                invoice = self.env['account.move'].search([('name','=',invoice_number)])
                if invoice != None and index !=0 :
                    day= self.env['config.day'].search([('date','=',invoice.date_invoice),('branch_id','=',invoice.branch_id.id)])
                    if invoice.move_type != 'out_refund':
                        self.env['config.day.line'].create({ 'invoices_number': invoice.name,
                        'untaxed_amount':invoice.amount_untaxed,
                           'total_amount':invoice.amount_total,
                           'tax_amount':invoice.amount_tax,
                           'new_untaxed_amount':invoice.amount_untaxed,
                           'new_tax_amount':invoice.amount_tax,
                           'new_total_amount':invoice.amount_total,
                           'date':invoice.date_invoice,
                            'branch_id': invoice.branch_id.id,
                            'config_day_id':day.id,
                            'confirm': True,})
                    else:
                        self.env['config.day.line'].create({'invoices_number': invoice.name,
                                                            'untaxed_amount': -1*invoice.amount_untaxed,
                                                            'total_amount': -1*invoice.amount_total,
                                                            'tax_amount': -1*invoice.amount_tax,
                                                            'new_untaxed_amount': -1*invoice.amount_untaxed,
                                                            'new_tax_amount': -1*invoice.amount_tax,
                                                            'new_total_amount': -1*invoice.amount_total,
                                                            'date': invoice.date_invoice,
                                                            'branch_id': invoice.branch_id.id,
                                                            'config_day_id': day.id,
                                                            'confirm': True, })
                    day.action_update_madinty()

        else:
            raise ValidationError (
                _ ("Upload Sheet"))



   
    def send_day( self ):
        for rec in self:
            if self._cr.dbname == "live_11nov_2024" :
                if rec.file:
                    rec.import_excel()
                else:
                    mail_days = self.env['config.day'].search([('date','=',rec.date),('is_post','=',False)])


                    for days_to_send in mail_days:
                        url = "https://api.tradelinestores.net/StoresMallsintegrations/GetTransactions"
                        headers = {"Content-Type" : "application/json"}
                        store_list_id = []
                        store_list_name = []
                        if days_to_send.branch_id.id not in store_list_id :
                            store_list_id.append (days_to_send.branch_id.id)
                            store_list_name.append (days_to_send.branch_id.name)

                        for store_id in store_list_id :
                            # days_of_store = self.env['config.day'].search([('state', '=', 'done'),('date','=',fields.Date.today() -  timedelta(days=1)), ('is_post', '=', False),('branch_id','=',store_id)])
                            days_of_store = self.env ['config.day'].search (
                                [('date', '=', rec.date), ('is_post', '=', False),
                                 ('branch_id', '=', store_id)])

                            if days_of_store :
                                days_line_of_store = self.env ['config.day.line'].search (
                                    [('config_day_id', '=', days_of_store.id)])

                                mall_integration = self.env['base.integration'].search([('branch', '=', store_id)])
                                if mall_integration:
                                    # pass
                                    mall_integration._prepare_invoices(days_line_of_store)
                                    days_of_store.is_post = True
                                    days_of_store.state = 'done'
                                else:
                                    mallsTransactionsItems = days_of_store._prepare_invoices_day_line (days_line_of_store)
                                    new_dict = {'StoreId' : store_id,
                                                'StoreName' : store_list_name [store_list_id.index (store_id)],
                                                'mallsTransactionsItems' : mallsTransactionsItems}
                                    pload = json.dumps (new_dict)
                                    try :
                                        r = requests.post (url=url, data=pload, headers=headers,verify=False)

                                        days_of_store.is_post = True
                                        days_of_store.state = 'done'
                                        _logger.info (r)


                                    except Exception as e :

                                        _logger.info ("error at Post Api")
                                        _logger.info (e)


from odoo import fields, models, api

import json
import logging
_logger = logging.getLogger(__name__)

import requests
class AccountInvoice(models.Model):
    _inherit = 'account.move'

    def prepare_invoice(self,invoice,percentage,min_invoice):
        if invoice.amount_total >= min_invoice:
            division_percentage = percentage

            new_total_amount = ((division_percentage * invoice.amount_total) / 100)
            new_tax_amount = ((division_percentage * invoice.amount_tax) / 100)
            new_untaxed_amount = new_total_amount - new_tax_amount
            if invoice.type != 'out_refund':

                dec = { 'invoices_number': invoice.name,
                    'untaxed_amount':invoice.amount_untaxed,
                       'total_amount':invoice.amount_total,
                       'tax_amount':invoice.amount_tax,
                       'new_untaxed_amount':new_untaxed_amount,
                       'new_tax_amount':new_tax_amount,
                       'new_total_amount':new_total_amount,
                       'date':invoice.date_invoice,
                        'branch_id': invoice.branch_id.id,
                        'confirm': True,}
            else:
               dec= {  'invoices_number': invoice.name,
                    'untaxed_amount': -1 * invoice.amount_untaxed,
                 'total_amount': -1 * invoice.amount_total,
                 'tax_amount': -1 * invoice.amount_tax,
                 'new_untaxed_amount': -1 * new_untaxed_amount,
                 'new_tax_amount': -1 * new_tax_amount,
                 'new_total_amount': -1 * new_total_amount,
                 'date': invoice.date_invoice,
                 'branch_id': invoice.branch_id.id,'confirm': True }
        else:
            dec = {'date': invoice.date_invoice,
                   'branch_id': invoice.branch_id.id,
                   'invoices_number': invoice.name,
                   'untaxed_amount': invoice.amount_untaxed if invoice.amount_total_signed > 0 else -1 * invoice.amount_untaxed,
                   'total_amount': invoice.amount_total_signed,
                   'tax_amount': invoice.amount_tax if invoice.amount_total_signed > 0 else -1 * invoice.amount_tax,
                   'new_untaxed_amount': invoice.amount_untaxed if invoice.amount_total_signed > 0 else -1 * invoice.amount_untaxed,
                   'new_total_amount': invoice.amount_total_signed,
                   'new_tax_amount': invoice.amount_tax if invoice.amount_total_signed > 0 else -1 * invoice.amount_tax,
                   'confirm': True, }
        return dec

    def _prepare_invoices_day_line(self, days_line_of_store):
        mallsTransactionsItems_list = []
        for line in days_line_of_store:
            mallsTransactionsItems_dict = {}
            mallsTransactionsItems_dict.setdefault('Transaction_No', line.invoices_number)
            mallsTransactionsItems_dict.setdefault('Transaction_Date', line.date.strftime('%Y-%m-%d'))
            mallsTransactionsItems_dict.setdefault('Transaction_Gross', round(line.new_total_amount, 2))
            mallsTransactionsItems_dict.setdefault('Transaction_Net', round(line.new_untaxed_amount, 2))
            mallsTransactionsItems_dict.setdefault('Taxes', round(line.new_tax_amount, 2))
            mallsTransactionsItems_list.append(mallsTransactionsItems_dict)
        return mallsTransactionsItems_list

    # def action_post(self):
    #     res = super(AccountInvoice, self).action_post()
    #     # if self._cr.dbname == "live_11nov_2024":
    #
    #     for rec in self:
    #         if rec.branch_id.id == 29:
    #             url = "https://api.tradelinestores.net/StoresMallsintegrations/GetTransactions"
    #             headers = {"Content-Type": "application/json"}
    #             month = self.env['config.month'].search(
    #                 [('branch_id', '=', rec.branch_id.id), ('month_selection', '=', rec.date_invoice.month),('year','=',rec.date_invoice.year)],limit=1)
    #             dec = self.prepare_invoice(rec,month.percentage,month.min_invoice_amount)
    #
    #             day = self.env['config.day'].search(
    #                 [('branch_id','=',rec.branch_id.id), ('date', '=',rec.date_invoice )])
    #             # day.sudo(2).server_action_create_daily_table()
    #             confg_day_line = self.env['config.day.line'].sudo().create(dec)
    #             confg_day_line.config_day_id = day.id
    #             untaxed_amount =0
    #             tax_amount =0
    #             total_amount =0
    #             new_untaxed_amount =0
    #             new_tax_amount =0
    #             new_total_amount =0
    #             for line in day.config_day_lines:
    #                 untaxed_amount += line.untaxed_amount
    #                 tax_amount += line.tax_amount
    #                 total_amount += line.total_amount
    #                 new_untaxed_amount += line.new_untaxed_amount
    #                 new_tax_amount += line.new_tax_amount
    #                 new_total_amount += line.new_total_amount
    #             day.sudo(2).old_untaxed_total_day  = untaxed_amount
    #             day.sudo(2).old_tax_day = tax_amount
    #             day.sudo(2).old_total_amount =total_amount
    #             day.sudo(2).new_untaxed_total_day  = new_untaxed_amount
    #             day.sudo(2).new_tax_day = new_tax_amount
    #             day.sudo(2).new_total_day =new_total_amount
    #             day.sudo(2).state = 'done'
    #             mallsTransactionsItems = self._prepare_invoices_day_line(confg_day_line)
    #             new_dict = {'StoreId': rec.branch_id.id,
    #                         'StoreName':rec.branch_id.name,
    #                         'mallsTransactionsItems': mallsTransactionsItems}
    #             pload = json.dumps(new_dict)
    #             try:
    #                 r = requests.post(url=url, data=pload, headers=headers,verify=False)
    #
    #                 _logger.info(r)
    #
    #             except Exception as e:
    #                 _logger.info("error at Post Api")
    #                 _logger.info(e)
    #
    #     return res

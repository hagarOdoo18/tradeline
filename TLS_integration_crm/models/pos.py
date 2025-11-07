from odoo import fields, models, api

from odoo.fields import datetime
from datetime import timedelta
import json
import logging
import requests
import time
from dateutil.relativedelta import relativedelta


_logger = logging.getLogger(__name__)


class account_bank_line (models.Model) :
    _inherit = 'account.bank.statement.line'

    is_tvc = fields.Boolean (
        string='Is_tvc',
        required=False)

    is_point = fields.Boolean (
        string='Is_point',
        required=False)


class pos_order(models.Model):
    _inherit = 'pos.order'

    is_tvc = fields.Boolean(
         string='Is_tvc',
         required=False)

    is_installment = fields.Boolean(
         string='installment',
         required=False)

    is_point = fields.Boolean (
        string='Is_point',
        required=False)


    def _dict_invocie(self,tvc_pos,amount):
        tvc_dict = {

            "InvoiceCustomerName": tvc_pos.partner_id.name,

            "InvoiceCustomerPhone": str(tvc_pos.partner_id.mobile),

            "InvoiceCustomerEmail": str(tvc_pos.partner_id.email),

            "InvoiceNumber": str(tvc_pos.name),

            "InvoiceAmount": str(int(amount)),

            "InvoiceDate": str(datetime.today().date()),

            "InvoiceStore": str(tvc_pos.user_id.id),
            "ISRedemption": "0"

        }
        return tvc_dict

    def post_invoiced(self, tvc_poses):
        date2 = datetime.strftime(datetime.today().date(), "%Y-%m-%d 15:15:00")
        date = datetime.strftime(datetime.today().date(), "%Y-%m-%d 13:00:00")

        for tvc_pos in tvc_poses:

            if str(tvc_pos.date_order.time()) >= '11:00:00' and str(tvc_pos.date_order.time()) <= '13:15:00' :
                _logger.info (tvc_pos.date_order)
                _logger.info (tvc_pos.name)
                _logger.info(str(tvc_pos.date_order.time()))
                InvoiceAmount = 0
                installment = True
                sent = False
                item_card = ''
                discount = False
                for line in tvc_pos.lines:
                    if line.product_id.categ_id.id not in [11, 22, 23, 21]:
                        if line.discount == 0 :

                            InvoiceAmount += line.price_subtotal

                        else:
                            discount = True
                for payment in tvc_pos.statement_ids:
                    if payment.journal_id.payment_type not in ['cash','visa']:
                        sent =False
                        break
                    else:
                        sent = True
                if installment  and int(InvoiceAmount) != 0 and not discount and sent:
                    tvc_dict = self._dict_invocie(tvc_pos, int(InvoiceAmount)*14)
                    self.post_tvc_invoice(tvc_dict, tvc_pos, False, int(InvoiceAmount)*14, item_card)


    def post_tvc_invoice( self ,tvc_dict,tvc_invoice,tvc_so,InvoiceAmount,item_code):
        url = "https://api.tradelinestores.net/TVCIntegration/ImportOdooTVCInvoices"
        token = self.env['account.move']._get_token()
        tvc_invoice_obj ={}
        if token:
            headers = {'Authorization' : 'Bearer ' + token,
                       'Accept' : 'application/json',
                       'Content-Type' : 'application/json'}
            pload = json.dumps (dict(tvc_dict))
            if tvc_invoice:


                    if InvoiceAmount > 0:

                        tvc_invoice_obj = self.env ['account.invoice.tvc'].create ({
                            'sent_date' : datetime.today().date(),
                            'customer_number' : tvc_invoice.partner_id.mobile,
                            'invoice_number' : tvc_invoice.name,
                            'untaxed_amount' : InvoiceAmount,
                            'state' : 'draft'
                        })
            try:
                r = requests.post (url=url , data=pload , headers=headers , verify=False)

                if r.status_code == 200:
                    if tvc_invoice:

                        tvc_invoice.is_tvc = True
                        tvc_invoice.invoice_id.is_tvc=True
                        if tvc_invoice_obj:
                            tvc_invoice_obj.state = 'done'
                            tvc_invoice_obj.note = r.text
                            tvc_invoice_obj.card = item_code
                            self._cr.commit()
            except :
                if tvc_invoice_obj :

                    if 'state' in tvc_invoice_obj:
                        self._cr.commit ()

                        tvc_invoice_obj.state = 'error'
                        tvc_invoice_obj.note = r.text

                _logger.info ("error at Post Api")
        else:
            _logger.info ("error at Token")



    @api.model
    def sent_TVC_offer_invoice(self):
        if self._cr.dbname == "live_11nov_2024":
            invoice_date_today = datetime.today().date()
            if str(invoice_date_today) == '2024-02-14':
                tvc_poses = self.sudo().search([('partner_id.customer_type', '=', 'Individual'),('partner_id.national_id', '!=', False),('date_order', '>=', invoice_date_today),('is_tvc','=',True)
                                                ,('amount_total','>',0)], order='date_order')

                self.post_invoiced(tvc_poses)
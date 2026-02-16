from odoo import fields, models, api

from odoo.fields import datetime
from datetime import timedelta,date
import json
import logging
import requests
import time
from dateutil.relativedelta import relativedelta


_logger = logging.getLogger(__name__)




class AccountInvoice(models.Model):
    _inherit = 'account.move'


    is_tvc = fields.Boolean(
         string='Is_tvc',
         required=False)
    is_installment = fields.Boolean(
         string='is_installment',
         required=False)
    is_point = fields.Boolean(
         string='Is_point',
         required=False)

    def _get_token( self ):
        url = "https://api.tradelinestores.net/token"
        body = {
            'username' : "o0ezXV32640gTcJD4lQcdA==",
            'password' : "cvGNZZeDe6GutoqMsr4wuw==",
            'grant_type' : "password"
        }
        files = [

        ]
        headers = {
                   'Cookie':
                       '.AspNetCore.Session=CfDJ8Jmk3bOcAM5Gra9%2BuoaWcngZUBsiQsjqQ3owU084F4nJ0EX1qyhmhWhZX4Y7sF61vjDEaYna6bAl7sv%2BfBCgoIj2qoNc%2Bi8Qq%2BE8%2B6Sd7RvaMZSst92GSaX0D8%2FOUSpeL%2B%2BcctkLrqyGspctwG14cmCrgjb1spO7DirY3qKWTkpJ'
        }
        try:
            r = requests.post (url=url, data=body, headers=headers,files=files,verify=False)
            token = r.json ().get (u'access_token', False)
            return token
        except:
            return False

    def post_tvc_invoice( self ,tvc_dict,tvc_invoice,tvc_so,InvoiceAmount,item_code):
        url = "https://api.tradelinestores.net/TVCIntegration/ImportOdooTVCInvoices"
        token = self._get_token()
        tvc_invoice_obj ={}
        if token:
            headers = {'Authorization' : 'Bearer ' + token,
                       'Accept' : 'application/json',
                       'Content-Type' : 'application/json'}
            pload = json.dumps (dict(tvc_dict))
            if tvc_invoice:
                tvc_invoice_obj = self.env ['account.invoice.tvc'].search ([('invoice_number', '=', tvc_invoice.name)])
                if not tvc_invoice_obj  :
                    if InvoiceAmount > 0:
                        if tvc_invoice.move_type =='out_refund' :
                            tvc_invoice_obj = self.env ['account.invoice.tvc'].create ({
                                'sent_date' : datetime.today ().date (),
                                'customer_number' : tvc_invoice.partner_id.mobile,
                                'invoice_number' :  'Redemption-'+tvc_invoice.name,
                                'untaxed_amount' : InvoiceAmount,
                                'state' : 'draft',
                                'type':'credit'
                            })
                        else:
                            tvc_invoice_obj = self.env ['account.invoice.tvc'].create ({
                            'sent_date': datetime.today().date() ,
                            'customer_number' : tvc_invoice.partner_id.mobile,
                            'invoice_number' : tvc_invoice.name,
                            'untaxed_amount' : InvoiceAmount,
                            'state' : 'draft',
                            'type': 'debit'
                        })
                    else:
                        if tvc_invoice.move_type =='out_invoice'  and tvc_invoice.name  :
                            tvc_invoice_obj = self.env ['account.invoice.tvc'].create ({
                                'sent_date' : datetime.today ().date (),
                                'customer_number' : tvc_invoice.partner_id.mobile,
                                'invoice_number' : 'Redemption-' + tvc_invoice.name,
                                'untaxed_amount' : InvoiceAmount,
                                'state' : 'draft',
                                'type': 'debit'
                            })
                        else:
                            tvc_invoice_obj = self.env ['account.invoice.tvc'].create ({
                                'sent_date' : datetime.today ().date (),
                                'customer_number' : tvc_invoice.partner_id.mobile,
                                'invoice_number' : tvc_invoice.name,
                                'untaxed_amount' : InvoiceAmount,
                                'state' : 'draft',
                                'type': 'credit'
                            })
            else:
                tvc_so_obj = self.env ['account.invoice.tvc'].search (
                    [('invoice_number', '=', tvc_so.name)])
                if not tvc_so_obj :
                    if InvoiceAmount > 0:

                        tvc_invoice_obj = self.env ['account.invoice.tvc'].create ({
                            'sent_date' : datetime.today().date(),
                            'customer_number' : tvc_so.partner_id.mobile,
                            'invoice_number' : tvc_so.name,
                            'untaxed_amount' : InvoiceAmount,
                            'state' : 'draft'
                        })
                    else:
                        tvc_invoice_obj = self.env ['account.invoice.tvc'].create ({
                            'sent_date' : datetime.today ().date (),
                            'customer_number' : tvc_so.partner_id.mobile,
                            'invoice_number' :'Redemption-'+ tvc_so.name,
                            'untaxed_amount' : InvoiceAmount,
                            'state' : 'draft'
                        })

            try :
                r = requests.post (url=url , data=pload , headers=headers , verify=False)

                if r.status_code == 200:
                    if tvc_invoice:
                        if  str(tvc_invoice_obj.invoice_number).find('Redemption') == -1  :
                            tvc_invoice.is_tvc = True
                        else:
                            tvc_invoice.is_point = True
                        if tvc_invoice_obj:
                            tvc_invoice_obj.state = 'done'
                            tvc_invoice_obj.note = r.text
                            tvc_invoice_obj.card = item_code
                            self._cr.commit()
                    else:
                        if InvoiceAmount > 0 :
                            tvc_so.is_tvc = True
                        else :
                            tvc_so.is_point = True

                        if tvc_invoice_obj:
                            tvc_invoice_obj.state = 'done'
                            tvc_invoice_obj.note = r.text
                            tvc_invoice_obj.card = item_code
                            self._cr.commit ()

                else:
                    tvc_invoice_obj.note = r.status_code
                    self._cr.commit()
                    time.sleep (10)
                    _logger.info (r)
            except :
                if tvc_invoice_obj :

                    if 'state' in tvc_invoice_obj:
                        self._cr.commit ()
                        tvc_invoice_obj.state = 'error'
                        tvc_invoice_obj.note = r.text

                _logger.info ("error at Post Api")
        else:
            _logger.info ("error at Token")

    def _dict_invocie( self ,tvc_invoice,InvoiceAmount):
        if InvoiceAmount > 0:
            if tvc_invoice.move_type =='out_refund'  :

                tvc_dict = {

                    "InvoiceCustomerName" : tvc_invoice.partner_id.name,

                    "InvoiceCustomerPhone" : str (tvc_invoice.partner_id.mobile),

                    "InvoiceCustomerEmail" : str (tvc_invoice.partner_id.email),

                    "InvoiceNumber" : 'Redemption-'+str (tvc_invoice.name),

                    "InvoiceAmount" : str (int (InvoiceAmount)),

                    "InvoiceDate" : str (tvc_invoice.invoice_date),

                    "InvoiceStore" : str (tvc_invoice.branch_id.id),

                    "ISRedemption":"1"
                }
            else:
                tvc_dict = {

                "InvoiceCustomerName" : tvc_invoice.partner_id.name,

                "InvoiceCustomerPhone" : str (tvc_invoice.partner_id.mobile),

                "InvoiceCustomerEmail" : str(tvc_invoice.partner_id.email),

                "InvoiceNumber" : str(tvc_invoice.name),

                "InvoiceAmount" : str(int(InvoiceAmount)),

                "InvoiceDate" : str(tvc_invoice.invoice_date),

                "InvoiceStore" : str(tvc_invoice.branch_id.id),
                "ISRedemption" : "0"

            }
        else:
            if  tvc_invoice.move_type =='out_invoice':
                tvc_dict = {

                    "InvoiceCustomerName" : tvc_invoice.partner_id.name,

                    "InvoiceCustomerPhone" : str (tvc_invoice.partner_id.mobile),

                    "InvoiceCustomerEmail" : str (tvc_invoice.partner_id.email),

                    "InvoiceNumber" : 'Redemption-' + str (tvc_invoice.name),

                    "InvoiceAmount" : str (int (InvoiceAmount)),

                    "InvoiceDate" : str (tvc_invoice.invoice_date),

                    "InvoiceStore" : str (tvc_invoice.branch_id.id),
                    "ISRedemption" : "1",

                }
            else:
                tvc_dict = {

                    "InvoiceCustomerName" : tvc_invoice.partner_id.name,

                    "InvoiceCustomerPhone" : str (tvc_invoice.partner_id.mobile),

                    "InvoiceCustomerEmail" : str (tvc_invoice.partner_id.email),

                    "InvoiceNumber" : str (tvc_invoice.name),

                    "InvoiceAmount" : str (int (InvoiceAmount)),

                    "InvoiceDate" : str (tvc_invoice.invoice_date),

                    "InvoiceStore" : str (tvc_invoice.branch_id.id),
                    "ISRedemption" : "0"

                }



        return tvc_dict

    def _dict_invocie_offer( self ,tvc_invoice,InvoiceAmount,offer_key):
        if InvoiceAmount > 0 :
            if tvc_invoice.move_type =='out_refund'  :

                tvc_dict = {

                    "InvoiceCustomerName" : tvc_invoice.partner_id.name,

                    "InvoiceCustomerPhone" : str (tvc_invoice.partner_id.mobile),

                    "InvoiceCustomerEmail" : str (tvc_invoice.partner_id.email),

                    "InvoiceNumber" : 'Redemption-'+str (tvc_invoice.number),

                    "InvoiceAmount" : str (int (InvoiceAmount)),

                    "InvoiceDate" : str (tvc_invoice.invoice_date),

                    "InvoiceStore" : str (tvc_invoice.branch_id.id),
                    "ISRedemption":"1",
                    "OfferName":offer_key

                }
            else:
                tvc_dict = {

                "InvoiceCustomerName" : tvc_invoice.partner_id.name,

                "InvoiceCustomerPhone" : str(tvc_invoice.partner_id.mobile),

                "InvoiceCustomerEmail" : str(tvc_invoice.partner_id.email),

                "InvoiceNumber" : str(tvc_invoice.number),

                "InvoiceAmount" : str(int(InvoiceAmount)),

                "InvoiceDate" : str(tvc_invoice.invoice_date),

                "InvoiceStore" : str(tvc_invoice.branch_id.id),
                "ISRedemption" : "0",
                "OfferName" : offer_key

                }
        else:
            if  tvc_invoice.move_type =='out_invoice':
                tvc_dict = {

                    "InvoiceCustomerName" : tvc_invoice.partner_id.name,

                    "InvoiceCustomerPhone" : str (tvc_invoice.partner_id.mobile),

                    "InvoiceCustomerEmail" : str (tvc_invoice.partner_id.email),

                    "InvoiceNumber" : 'Redemption-' + str (tvc_invoice.number),

                    "InvoiceAmount" : str (int (InvoiceAmount)),

                    "InvoiceDate" : str (tvc_invoice.invoice_date),

                    "InvoiceStore" : str (tvc_invoice.branch_id.id),
                    "ISRedemption" : "1",
                    "OfferName" : offer_key

                }
            else:
                tvc_dict = {

                    "InvoiceCustomerName" : tvc_invoice.partner_id.name,

                    "InvoiceCustomerPhone" : str (tvc_invoice.partner_id.mobile),

                    "InvoiceCustomerEmail" : str (tvc_invoice.partner_id.email),

                    "InvoiceNumber" : str (tvc_invoice.number),

                    "InvoiceAmount" : str (int (InvoiceAmount)),

                    "InvoiceDate" : str (tvc_invoice.invoice_date),

                    "InvoiceStore" : str (tvc_invoice.branch_id.id),
                    "ISRedemption" : "0",
                    "OfferName" : offer_key

                }



        return tvc_dict

    def _dict_order( self ,tvc_order,orderAmount):
        if orderAmount > 0:

            tvc_dict = {

                "InvoiceCustomerName" : tvc_order.partner_id.name,

                "InvoiceCustomerPhone" : str(tvc_order.partner_id.mobile),

                "InvoiceCustomerEmail" : str(tvc_order.partner_id.email),

                "InvoiceNumber" : str(tvc_order.name),

                "InvoiceAmount" : str(int(orderAmount)),

                "InvoiceDate" : str(tvc_order.date_order),

                "InvoiceStore" : str(tvc_order.branch_id.id),
                "ISRedemption" : "0",

            }
        else:

            tvc_dict = {

                "InvoiceCustomerName" : tvc_order.partner_id.name,

                "InvoiceCustomerPhone" : str (tvc_order.partner_id.mobile),

                "InvoiceCustomerEmail" : str (tvc_order.partner_id.email),

                "InvoiceNumber" :  'Redemption-'+str (tvc_order.name),

                "InvoiceAmount" : str (int (orderAmount)),

                "InvoiceDate" : str (tvc_order.date_order),

                "InvoiceStore" : str (tvc_order.branch_id.id),
                "ISRedemption" : "1",

            }

        return tvc_dict
    def _dict_order_offer( self ,tvc_order,orderAmount,offer_key):
        if orderAmount > 0:

            tvc_dict = {

                "InvoiceCustomerName" : tvc_order.partner_id.name,

                "InvoiceCustomerPhone" : str(tvc_order.partner_id.mobile),

                "InvoiceCustomerEmail" : str(tvc_order.partner_id.email),

                "InvoiceNumber" : str(tvc_order.name),

                "InvoiceAmount" : str(int(orderAmount)),

                "InvoiceDate" : str(tvc_order.date_order),

                "InvoiceStore" : str(tvc_order.branch_id.id),
                "ISRedemption" : "0",
                "OfferName" : offer_key

            }
        else:

            tvc_dict = {

                "InvoiceCustomerName" : tvc_order.partner_id.name,

                "InvoiceCustomerPhone" : str (tvc_order.partner_id.mobile),

                "InvoiceCustomerEmail" : str (tvc_order.partner_id.email),

                "InvoiceNumber" :  'Redemption-'+str (tvc_order.name),

                "InvoiceAmount" : str (int (orderAmount)),

                "InvoiceDate" : str (tvc_order.date_order),

                "InvoiceStore" : str (tvc_order.branch_id.id),
                "ISRedemption" : "1",
                "OfferName" : offer_key

            }

        return tvc_dict

    def _get_card( self,customer_id,invoice_date ):

        black_cards = self.env['sale.order.line'].sudo().search([('order_id.state','=','sale'),('order_id.partner_id','=',customer_id.id),('qty_delivered','!=',0),
                                                  ('product_id.barcode','=','TLS-CARE-BLACK')])
        green_cards = self.env['sale.order.line'].sudo().search([('order_id.state','=','sale'),('order_id.partner_id','=',customer_id.id),('qty_delivered','!=',0),
                                                  ('product_id.barcode','=','TLS CARE GREEN')])
        Blue_cards  = self.env['sale.order.line'].sudo().search([('order_id.state','=','sale'),('order_id.partner_id','=',customer_id.id),('qty_delivered','!=',0),
                                                  ('product_id.barcode','=','Tls-BLU')])
        ORG_cards  = self.env['sale.order.line'].sudo().search([('order_id.state','=','sale'),('order_id.partner_id','=',customer_id.id),('qty_delivered','!=',0),
                                                  ('product_id.barcode','=','TLS-ORG')])
        card = False

        for green_card in green_cards:
            if green_card:

                expiration_date = green_card.order_id.date_order.date + relativedelta (years=1)
                if expiration_date >= invoice_date:
                    card = 'green'
        for green_card in green_cards:
            if green_card:

                expiration_date = green_card.order_id.date_order.date + relativedelta(years=1)
                if expiration_date >= invoice_date:
                    card = 'green'
        for Blue_card in Blue_cards:
            if Blue_card:

                expiration_date = Blue_card.order_id.date_order.date + relativedelta(years=1)
                if expiration_date >= invoice_date:
                    card = 'Blue'
        for org_card in ORG_cards:
            if org_card:

                expiration_date = org_card.order_id.date_order.date + relativedelta(years=1)
                if expiration_date >= invoice_date:
                    card = 'ORG'
        for black_card in black_cards:
            if black_card:
                if black_card.qty_delivered ==  2 and black_card.order_date.strftime("%Y-%m-%d 00:00:00") >= '2022-03-03':
                    expiration_date = black_card.order_id.date_order.date + relativedelta (years=2)
                    if expiration_date >= invoice_date :
                        card = 'black'
                else:
                    expiration_date = black_card.order_id.date_order.date + relativedelta(years= 1)
                    if expiration_date >= invoice_date:
                        card = 'black'
        black_cards = self.env ['pos.order.line'].sudo ().search (
            [('order_id.state', '=', 'invoiced'), ('order_id.partner_id', '=', customer_id.id),
             ('product_id.barcode', '=', 'TLS-CARE-BLACK')])
        green_cards = self.env ['pos.order.line'].sudo ().search (
            [('order_id.state', '=', 'invoiced'), ('order_id.partner_id', '=', customer_id.id),
             ('product_id.barcode', '=', 'TLS CARE GREEN')])
        Blue_cards = self.env['pos.order.line'].sudo().search(
            [('order_id.state', '=', 'invoiced'), ('order_id.partner_id', '=', customer_id.id),
             ('product_id.barcode', '=', 'Tls-BLU')])
        ORG_cards = self.env['pos.order.line'].sudo().search(
            [('order_id.state', '=', 'invoiced'), ('order_id.partner_id', '=', customer_id.id),
             ('product_id.barcode', '=', 'TLS-ORG')])
        for green_card in green_cards :
            if green_card :

                expiration_date = green_card.order_id.date_order.date() + relativedelta(years=1)
                if expiration_date >= invoice_date :
                    card = 'green'
        for Blue_card in Blue_cards:
            if Blue_card:

                expiration_date = Blue_card.order_id.date_order + relativedelta(years=1)
                if expiration_date >= invoice_date:
                    card = 'Blue'
        for org_card in ORG_cards:
            if org_card:

                expiration_date = org_card.order_id.date_order + relativedelta(years=1)
                if expiration_date >= invoice_date:
                    card = 'ORG'
        for black_card in black_cards :
            if black_card :
                if black_card.qty == 2 and black_card.order_id.date_order.strftime("%Y-%m-%d 00:00:00") >= '2022-03-03':
                    expiration_date = black_card.order_id.date_order.date() + relativedelta(years=2)
                    if expiration_date >= invoice_date :
                        card = 'black'
                else:
                    expiration_date = black_card.order_id.date_order.date() + relativedelta(years=1)
                    if expiration_date >= invoice_date :
                        card = 'black'

        return card

    def post_invoiced( self ,tvc_invoices):
        for tvc_invoice in tvc_invoices :
            if tvc_invoice.partner_id.vat  or tvc_invoice.branch_id.company_id.id == 6 and tvc_invoice.branch_id.id not in [64,88,89,91,90,39]  :
                InvoiceAmount = 0
                card = False
                installment = True
                sent = False
                item_card = ''
                discount = False
                offer = False
                xprs_offer = False
                if self._get_card (tvc_invoice.partner_id, tvc_invoice.invoice_date) :
                    item_card = self._get_card (tvc_invoice.partner_id, tvc_invoice.invoice_date)
                    card = True

                for line in tvc_invoice.invoice_line_ids :
                    if line.product_id.categ_id.id not in [ 36, 53,55,50] :
                        if line.discount == 0   :

                            InvoiceAmount += line.price_subtotal
                        # elif line.product_id.categ_id.id == 4  and  item_card == 'black' :
                        #     InvoiceAmount += line.price_subtotal

                        else:
                            discount =True

                today = date.today()

                # offer_object = self.env['tvc.offer'].search(
                #     [('branches', 'in', tvc_invoice.user_id.id), ('start_date', '<=', fields.Date.to_string(today)), ('end_date', '>=', fields.Date.to_string(today))
                #         , ('min_amount', '<', InvoiceAmount),], limit=1)

                payments = tvc_invoice._get_reconciled_payments()
                if payments:
                    for payment in payments :
                        if  payment.journal_id.payment_type:

                            if payment.journal_id.payment_type in ['installment', 'pints', 'withholding_tax', 'wallet', 'Trade-In',
                                                                   'credit', 'voucher'] and card :
                                installment = True
                                sent = True
                            elif payment.journal_id.payment_type in ['installment', 'pints', 'withholding_tax', 'wallet',
                                                                     'Trade-In', 'credit', 'voucher'] and not card :
                                installment = False
                                sent = False
                            elif payment.journal_id.payment_type not in ['installment', 'pints', 'withholding_tax', 'wallet',
                                                                         'Trade-In', 'credit', 'voucher'] :
                                sent = True

                            if payment.journal_id.payment_type == 'TVC' :

                                tvc_amount = payment.amount / 1.14
                                InvoiceAmount -= tvc_amount

                            if payment.journal_id.id == 386:
                                Premium_offer = True

                            if payment.journal_id.payment_type in ['visa','cash','installment','wallet','Trade-In','credit'] and tvc_invoice.branch_id.id ==6 :
                                xprs_offer = True
                            # for type in offer_object.payment_types:
                            #
                            #     if payment.journal_id.payment_type == type.name:
                            #         offer =True
                            #         break
                            # if payment.journal_id.id in offer_object.journal_ids.ids:
                            #     offer = True
                else:
                    for payment in tvc_invoice.pos_order_ids.payment_ids:
                        if payment.payment_method_id.journal_id.payment_type:
                            if tvc_invoice.branch_id.id == 1:
                                if payment.payment_method_id.journal_id.payment_type in ['installment', 'pints', 'withholding_tax',
                                                                       'wallet', 'Trade-In',
                                                                       'credit', 'voucher'] and card:
                                    installment = True
                                    sent = True
                                elif payment.payment_method_id.journal_id.payment_type in ['installment', 'pints', 'withholding_tax',
                                                                         'wallet',
                                                                         'Trade-In', 'credit', 'voucher'] and not card:
                                    installment = False
                                    sent = False
                                elif payment.payment_method_id.journal_id.payment_type not in ['installment', 'pints', 'withholding_tax',
                                                                             'wallet',
                                                                             'Trade-In', 'credit', 'voucher']:
                                    sent = True
                            else:

                                if payment.payment_method_id.journal_id.payment_type in ['pints', 'withholding_tax',

                                                                       'voucher'] and card:
                                    installment = True
                                    sent = True
                                elif payment.payment_method_id.journal_id.payment_type in ['pints', 'withholding_tax',

                                                                         'voucher'] and not card:
                                    installment = False
                                    sent = False
                                elif payment.payment_method_id.journal_id.payment_type not in ['pints', 'withholding_tax',

                                                                             'voucher']:
                                    sent = True
                            if payment.payment_method_id.journal_id.payment_type == 'TVC':

                                tvc_amount = payment.amount / 1.14
                                InvoiceAmount -= tvc_amount

                            if payment.payment_method_id.journal_id.id == 386:
                                Premium_offer = True

                            if payment.payment_method_id.journal_id.payment_type in ['visa', 'cash', 'installment', 'wallet', 'Trade-In',
                                                                   'credit'] and tvc_invoice.branch_id.company_id.id ==6:
                                xprs_offer = True
                            # for type in offer_object.payment_types:
                            #
                            #     if payment.journal_id.payment_type == type.name:
                            #         offer = True
                            #         break
                            # if payment.journal_id.id in offer_object.journal_ids.ids:
                            #     offer = True

                # if Premium_offer :
                #
                #     InvoiceAmount = 2 * InvoiceAmount

                # if offer:
                #     InvoiceAmount = InvoiceAmount * offer_object.amount


                # if  xprs_offer and int(InvoiceAmount)!=0:
                #     # InvoiceAmount = 5 * InvoiceAmount
                #     point = int(InvoiceAmount)
                #
                #     tvc_invoice.send_smss(tvc_invoice.partner_id.mobile, point)

                if installment and sent and int(InvoiceAmount)!=0 and not discount:
                    tvc_dict = self._dict_invocie(tvc_invoice, int (InvoiceAmount))
                    self.post_tvc_invoice(tvc_dict, tvc_invoice,False, int (InvoiceAmount), item_card)


    def post_invoiced_offer( self ,tvc_invoices,offer_key):

        item_card = ''
        discount = False
        for tvc_invoice in tvc_invoices :

            # if tvc_invoice.user_id.is_branch:
            #     for line in tvc_invoice.invoice_line_ids :
            #         if line.amount_dis != 0 or line.discount != 0 or line.product_id.sub_category_id.id in [71, 72] :
            #             discount = True
            #     if not discount:
            #         if self._get_card (tvc_invoice.partner_id, tvc_invoice.invoice_date) :
            #             item_card = self._get_card (tvc_invoice.partner_id, tvc_invoice.invoice_date)
            #         tvc_amount = 0
            #         for payment in tvc_invoice.payment_move_line_ids :
            #             if offer_key == 'Offer11To13August2022':
            #                 if payment.journal_id.id == 680:
            #                     payment_amount = payment.credit
            #
            #                     tvc_amount += int(payment_amount)
            #             else:
            #                 if payment.journal_id.offer  :
            #                     payment_amount = payment.credit
            #
            #                     tvc_amount += int (payment_amount)
            #
            #         if int(tvc_amount) !=0 :
            if offer_key == 'Offer13october2022aljazeera':
                if self._get_card(tvc_invoice.partner_id, tvc_invoice.invoice_date):
                     item_card = self._get_card (tvc_invoice.partner_id, tvc_invoice.invoice_date)
                tvc_dict = self._dict_invocie_offer(tvc_invoice, int (1000),offer_key)
                self.post_tvc_invoice(tvc_dict, tvc_invoice,False, int (1000), item_card)
    def post_invoiced_credit_offer( self ,tvc_invoices,offer_key):

        item_card = ''
        discount = False
        for tvc_invoice in tvc_invoices :

            if tvc_invoice.user_id.is_branch:
                for line in tvc_invoice.invoice_line_ids :
                    if line.amount_dis != 0 or line.discount != 0 or line.product_id.sub_category_id.id in [71, 72] :
                        discount = True
                if not discount:
                    if self._get_card (tvc_invoice.partner_id, tvc_invoice.invoice_date) :
                        item_card = self._get_card (tvc_invoice.partner_id, tvc_invoice.invoice_date)
                    tvc_amount = 0
                    for payment in tvc_invoice.payment_move_line_ids :
                        if payment.journal_id.id == 680:
                            payment_amount = payment.credit

                            tvc_amount += int(payment_amount)

                    if int(tvc_amount) !=0 :
                        tvc_dict = self._dict_invocie_offer(tvc_invoice, -1*int (tvc_amount),offer_key)
                        self.post_tvc_invoice(tvc_dict, tvc_invoice,False, -1* int (tvc_amount), item_card)

    def post_credit( self ,tvc_credits):

        for tvc_credit in tvc_credits :
            InvoiceAmount = 0
            card = False
            installment = True
            sent = False
            item_card =""
            discount = False
            tvc_amount=0
            if self._get_card (tvc_credit.partner_id,tvc_credit.invoice_date) :
                item_card = self._get_card (tvc_credit.partner_id,tvc_credit.invoice_date)
                card = True
            if not tvc_credit.offer:
                for line in tvc_credit.invoice_line_ids :
                    if line.product_id.categ_id.id not in [36, 53, 55, 50]:
                        if line.discount == 0 and line.amount_dis ==0:
                            InvoiceAmount += line.price_subtotal
                        # elif line.product_id.categ_id.id == 4 and item_card == 'black' :
                        #     InvoiceAmount += line.price_subtotal

                        else:
                            discount=True
                if InvoiceAmount ==0:
                    pass
                payments = tvc_credit._get_reconciled_payments()
                if payments:
                    for payment in payments:
                        if payment.journal_id.payment_type:
                            if tvc_credit.branch_id.id == 1:
                                if payment.journal_id.payment_type in ['installment', 'pints', 'withholding_tax',
                                                                       'wallet', 'Trade-In',
                                                                       'credit', 'voucher'] and card:
                                    installment = True
                                    sent = True
                                elif payment.journal_id.payment_type in ['installment', 'pints', 'withholding_tax',
                                                                         'wallet',
                                                                         'Trade-In', 'credit', 'voucher'] and not card:
                                    installment = False
                                    sent = False
                                elif payment.journal_id.payment_type not in ['installment', 'pints', 'withholding_tax',
                                                                             'wallet',
                                                                             'Trade-In', 'credit', 'voucher']:
                                    sent = True
                            else:

                                if payment.journal_id.payment_type in ['pints', 'withholding_tax',

                                                                       'voucher'] and card:
                                    installment = True
                                    sent = True
                                elif payment.journal_id.payment_type in ['pints', 'withholding_tax',

                                                                         'voucher'] and not card:
                                    installment = False
                                    sent = False
                                elif payment.journal_id.payment_type not in ['pints', 'withholding_tax',

                                                                             'voucher']:
                                    sent = True
                            if payment.journal_id.payment_type == 'TVC':
                                tvc_amount = payment.amount / 1.14
                                InvoiceAmount -= tvc_amount

                            if payment.journal_id.id == 386:
                                Premium_offer = True

                            if payment.journal_id.payment_type in ['visa', 'cash', 'installment', 'wallet', 'Trade-In',
                                                                   'credit'] and tvc_credit.branch_id.id == 6:
                                xprs_offer = True
                            # for type in offer_object.payment_types:
                            #
                            #     if payment.journal_id.payment_type == type.name:
                            #         offer = True
                            #         break
                            # if payment.journal_id.id in offer_object.journal_ids.ids:
                            #     offer = True
                else:
                    for payment in tvc_credit.pos_order_ids.payment_ids:
                        if payment.payment_method_id.journal_id.payment_type:
                            if tvc_credit.branch_id.id == 1:
                                if payment.payment_method_id.journal_id.payment_type in ['installment', 'pints',
                                                                                         'withholding_tax',
                                                                                         'wallet', 'Trade-In',
                                                                                         'credit', 'voucher'] and card:
                                    installment = True
                                    sent = True
                                elif payment.payment_method_id.journal_id.payment_type in ['installment', 'pints',
                                                                                           'withholding_tax',
                                                                                           'wallet',
                                                                                           'Trade-In', 'credit',
                                                                                           'voucher'] and not card:
                                    installment = False
                                    sent = False
                                elif payment.payment_method_id.journal_id.payment_type not in ['installment', 'pints',
                                                                                               'withholding_tax',
                                                                                               'wallet',
                                                                                               'Trade-In', 'credit',
                                                                                               'voucher']:
                                    sent = True
                            else:

                                if payment.payment_method_id.journal_id.payment_type in ['pints', 'withholding_tax',

                                                                                         'voucher'] and card:
                                    installment = True
                                    sent = True
                                elif payment.payment_method_id.journal_id.payment_type in ['pints', 'withholding_tax',

                                                                                           'voucher'] and not card:
                                    installment = False
                                    sent = False
                                elif payment.payment_method_id.journal_id.payment_type not in ['pints',
                                                                                               'withholding_tax',

                                                                                               'voucher']:
                                    sent = True
                            if payment.payment_method_id.journal_id.payment_type == 'TVC':
                                tvc_amount = payment.amount / 1.14
                                InvoiceAmount -= tvc_amount

                            if payment.journal_id.id == 386:
                                Premium_offer = True

                            if payment.journal_id.payment_type in ['visa', 'cash', 'installment', 'wallet', 'Trade-In',
                                                                   'credit'] and tvc_credit.branch_id.company_id.id == 6:
                                xprs_offer = True
                            # for type in offer_object.payment_types:
                            #
                            #     if payment.journal_id.payment_type == type.name:
                            #         offer = True
                            #         break
                            # if payment.journal_id.id in offer_object.journal_ids.ids:
                            #     offer = True

                InvoiceAmount -= tvc_amount
                if installment and sent and int (InvoiceAmount) != 0 and not discount :
                    tvc_dict = self._dict_invocie (tvc_credit, int (-1 * InvoiceAmount))
                    self.post_tvc_invoice (tvc_dict, tvc_credit, False, int (-1 * InvoiceAmount), item_card)
                    # if item_card =='black':
                    #     tvc_credit.partner_id.tvc_points -= int( InvoiceAmount*2)
                    # else:
                    #     tvc_credit.partner_id.tvc_points -= int( InvoiceAmount)
                else :
                    tvc_credit.is_installment = True
            else:
                product_item_code = ['AS-NG-G512LV-ES74', 'FX517ZM-AS73', 'FX517ZZR-F15-I73070', 'T3300KA-OLED001W',
                                     '90NB0S3B-M04830', 'TP412FA-4G003T', 'AN515-45-R0AX'
                    , 'AN515-45-R0AX/16G', 'G15-001-DGRY', 'LAT-3520-Ci5', 'LAT-5520-Ci7', 'CUS2130SH', '81WB00S0ED',
                                     '81WB0104ED', '53011WGC', '38M25AV', '204K7EA#ABV'
                    , '82JU00DYED', '82JU00E0ED', '82N600Q3ED', '81YT0000US', '82K800E3ED', '81YU0088ED', '82K200MHED',
                                     'GF65071', 'GF65092', 'GF65092/16G', 'GF63-11SC-224'
                    , 'GF63-11UC-262', '21O-00001', 'SM-S908EDRG/KSH', 'SM-S908EZGG/AN', 'SM-S908EZKG/KSH',
                                     'SM-S908EZKG/SH', 'SM-F711BZEEMEA/69', 'SM-F936BZK/BLK',
                                     'SM-F93BZE/BEIG', 'SM-F936BZA/GRY', 'P-27418978-S/HE', 'P27418872L',
                                     'P27418872L/SB', 'P27418948P', 'P27418948P/BL', ]
                item_code = False

                # offer_key = 'Offer11To13August2022'
                # offer_key = 'Offer20To22June2022'
                offer = True
                if tvc_credit.user_id.id == 110:
                    for line in tvc_credit.invoice_line_ids:

                        if line.discount == 0 and line.amount_dis == 0:
                            discount =True
                        if line.product_id.barcode in product_item_code:
                            item_code=True
                    for payment in tvc_credit.payment_move_line_ids:

                        # if payment.journal_id.id ==  680:
                        if payment.journal_id.payment_type in ['cash', 'visa']:
                            # tvc_amount = ( payment.amount)
                            # saleAmount += tvc_amount
                            pass
                        else:
                            offer = False

                    if offer and not discount and item_code:
                            # offer_key = 'Offer11To13August2022'
                            # offer_key = 'Offer20To22June2022'
                            offer_key = 'Offer13october2022aljazeera'

                            tvc_dict = self._dict_invocie_offer (tvc_credit, int (-1 * 1000),offer_key)
                            self.post_tvc_invoice (tvc_dict, tvc_credit, False, int (-1 * 1000), item_card)



    def post_so( self,tvc_sro_orders ):


        for tvc_sro_order in tvc_sro_orders :
            if tvc_sro_order.partner_id.national_id:
                saleAmount = 0
                card = False
                installment = True
                sent = False
                item_card =""
                discount = False
                payments = self.env ['account.payment'].sudo ().search ([('sale_order_id', '=', tvc_sro_order.id),('state','!=','canceled')])

                if self._get_card (tvc_sro_order.partner_id, tvc_sro_order.create_date) :
                    item_card = self._get_card (tvc_sro_order.partner_id, tvc_sro_order.create_date)
                    card = True

                for line in tvc_sro_order.sudo ().order_line :

                    if line.product_id.categ_id.id not in [ 11,22,23,21] :

                        if line.discount == -14 and line.product_id.categ_id.id not in  [25,26]  :
                            saleAmount += line.new_subtotal / 1.14
                        # elif line.product_id.categ_id.id == 4 and item_card == 'black' :
                        #     saleAmount += line.new_subtotal / 1.14
                        else:
                            discount =True

                if payments :
                    for payment in payments :
                        if payment.journal_id.payment_type:
                            if payment.journal_id.payment_type in ['installment', 'pints', 'withholding_tax', 'wallet',
                                                                   'Trade-In', 'credit', 'voucher'] and card :
                                installment = True
                                sent = True
                            elif payment.journal_id.payment_type in ['installment', 'pints', 'withholding_tax', 'wallet',
                                                                     'Trade-In', 'credit', 'voucher'] and not card :
                                installment = False
                                sent = False
                            elif payment.journal_id.payment_type not in ['installment', 'pints', 'withholding_tax',
                                                                         'wallet',
                                                                         'Trade-In', 'credit', 'voucher'] :
                                sent = True

                            if payment.journal_id.payment_type == 'TVC'   :

                                tvc_amount = payment.amount / 1.14
                                saleAmount -= tvc_amount
                            if payment.journal_id.id == 386:
                                saleAmount = 2 * saleAmount
                            if payment.journal_id.payment_type == 'cash' and tvc_sro_order.company_id.id == 6:
                                saleAmount = 5*saleAmount
                    if installment and sent and int(saleAmount)!=0 and not discount:
                        tvc_dict = self._dict_order (tvc_sro_order, int (saleAmount))

                        self.post_tvc_invoice (tvc_dict,False, tvc_sro_order, int (saleAmount), item_card)
                        # if item_card == 'black' :
                        #     tvc_sro_order.partner_id.tvc_points += int (saleAmount * 2)
                        # else :
                        #     tvc_sro_order.partner_id.tvc_points += int (saleAmount)
    def post_so_offer( self,tvc_sro_orders ,offer_key):


        for tvc_sro_order in tvc_sro_orders :
            discount = False
            if tvc_sro_order.user_id.is_branch:
                for line in tvc_sro_order.sudo ().order_line :
                    if line.discount > 0 or line.product_id.sub_category_id.id in [71, 72] :
                        discount = True
                if not discount:
                    item_card =""

                    payments = self.env ['account.payment'].sudo ().search ([('order_id', '=', tvc_sro_order.id)])

                    if self._get_card (tvc_sro_order.partner_id, tvc_sro_order.create_date) :
                        item_card = self._get_card (tvc_sro_order.partner_id, tvc_sro_order.create_date)

                    if payments :
                        for payment in payments :
                            if offer_key == 'Offer11To13August2022':
                                if payment.journal_id.id == 680 :
                                    tvc_amount = payment.amount

                            else:
                                if  payment.journal_id.offer:

                                    tvc_amount = payment.amount

                        if int(tvc_amount)!=0 :
                            tvc_dict = self._dict_order_offer (tvc_sro_order, int (tvc_amount),offer_key)

                            self.post_tvc_invoice (tvc_dict,False, tvc_sro_order, int (tvc_amount), item_card)

    def post_tvc_point( self ,tvc_bank_pays):
        all_tvc_payment= {}
        for payment in tvc_bank_pays :

            tvcAmount = 0

            item_card = ''

            if self._get_card(payment.partner_id,payment.date) :
                item_card = self._get_card(payment.partner_id,payment.date)

            if payment.journal_id.payment_type == 'TVC' :
                if not payment.is_point:
                    tvc_invoice = self.env['account.move'].search([('invoice_origin', '=', payment.pos_statement_id.name)])
                    payment.is_point = True
                    tvcAmount =  payment.amount * -1
                    if tvc_invoice.number not in all_tvc_payment.keys():
                        all_tvc_payment.setdefault(tvc_invoice.number,tvcAmount)
                    else:
                        all_tvc_payment[tvc_invoice.number] += tvcAmount


        for tvc_payment in all_tvc_payment.keys():
            tvc_invoice = self.search([('number','=',tvc_payment)])
            if int(all_tvc_payment[tvc_payment])!=0 and tvc_invoice  :
                if tvc_invoice.is_point ==False:
                    tvc_dict = self._dict_invocie(tvc_invoice, int (all_tvc_payment[tvc_payment]))
                    self.post_tvc_invoice(tvc_dict, tvc_invoice,False, int (all_tvc_payment[tvc_payment]), item_card)
    
    def send_smss(self, customer_mobile, point):
        to_day = fields.date.today()
        start_date = to_day + timedelta(days=30)

        date = start_date.strftime('%d/%m/%Y')
        # amount = int(point / 100)

        url = "https://apis.cequens.com/auth/v1/tokens/"

        payload = {
            "apiKey": "bc36c111-e641-467f-91c1-9f2982fb5f0d",
            "userName": "Trade line"
        }
        headers = {
            "accept": "application/json",
            "content-type": "application/json"
        }

        response = requests.post(url, json=payload, headers=headers)
        token =  response.json ().get ('data',False)['access_token']

        url_swq = "https://apis.cequens.com/sms/v1/messages"
        massage = "عزيزي عميل XPRS" +"\n"+"برجاء العلم انه تم اضافه"+str(point)+"نقاط الي حسابك"+"\n"+"لمعرفه القيمه النقديه لهذه النقاط برجاء الاتصال بنا علي 19857"
        url_sms = "https://smsmisr.com/api/webapi/?Username=GEBB3KC6&password=S9UA06&language=1&sender=XPRS" + "&mobile=" + str(
            customer_mobile) + "&message= " + massage

        # try :
        #     r = requests.post (url_sms)
        #     return True
        # except :
        #     pass
        payload = {
            "senderName": "XPRS",
            "messageType": "text",
            "acknowledgement": 0,
            "flashing": 0,
            "recipients": str(customer_mobile),
            "messageText": massage
        }
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": "Bearer "+token
        }
        try:
            response = requests.post(url=url_swq, json=payload, headers=headers)
            # if response.json()['replyCode'] != 0:
            #     massage = "Thank you for your purchase at XPRS. You have earned " + str(point) + " Points (" + str(
            #         amount) + " EGP), you can use them starting " + str(
            #         date) + ".                  Download our App now and check your loyalty points.        http://onelink.to/9cj2wn"
            #
            #     url_sms = "https://smsmisr.com/api/webapi/?Username=GEBB3KC6&password=S9UA06&language=1&sender=Tradeline" + "&mobile=" + str(
            #         customer_mobile) + "&message= " + massage
            #
            #     try:
            #         r = requests.post(url_sms)
            #
            #     except
            #         pass
        except  :
            pass
            # massage = "Thank you for your purchase at XPRS. You have earned " + str(point) + " Points (" + str(
            #     amount) + " EGP), you can use them starting " + str(
            #     date) + ".                  Download our App now and check your loyalty points.        http://onelink.to/9cj2wn"
            #
            # url_sms = "https://smsmisr.com/api/webapi/?Username=GEBB3KC6&password=S9UA06&language=1&sender=Tradeline" + "&mobile=" + str(
            #     customer_mobile) + "&message= " + massage
            #
            # try:
            #     r = requests.post(url_sms)
            # except:
            #     pass
        return True




    def post_pos_order_tvc_credit(self, pos_orders, ):

        for pos_order in pos_orders :
            card = False
            installment = True
            sent = False
            item_card = ''
            saleAmount=0
            tvc_amount=0
            discount =False
            if self._get_card(pos_order.partner_id,pos_order.date_order.date()) :
                item_card = self._get_card(pos_order.partner_id,pos_order.date_order.date())
                card=True
            if not pos_order.offer :
                for line in pos_order.lines:
                    if line.product_id.categ_id.id not in [36, 53, 55, 50]:
                        if line.discount == 0 :
                            saleAmount += line.price_subtotal
                        # elif line.product_id.categ_id.id == 4 and item_card == 'black' :
                        #     saleAmount += line.price_subtotal

                        else:
                            discount =True
                if saleAmount ==0:
                    pass
                for payment in pos_order.statement_ids :
                    if payment.journal_id.payment_type :
                        if payment.journal_id.payment_type in ['installment', 'pints', 'withholding_tax', 'wallet',
                                                               'Trade-In', 'credit', 'voucher'] and card :
                            installment = True
                            sent = True
                        elif payment.journal_id.payment_type in ['installment', 'pints', 'withholding_tax', 'wallet',
                                                                 'Trade-In', 'credit', 'voucher'] and not card :
                            installment = False
                            sent = False
                        elif payment.journal_id.payment_type not in ['installment', 'pints', 'withholding_tax',
                                                                     'wallet',
                                                                     'Trade-In', 'credit', 'voucher'] :
                            sent = True

                        if payment.journal_id.payment_type == 'TVC' :
                            untax_payment = (payment.amount/1.14)
                            tvc_amount += (-1 * untax_payment)
                        if payment.journal_id.id == 826:
                            saleAmount = 2 * saleAmount
                saleAmount += tvc_amount
                tvc_invoice = self.env ['account.move'].search ([('invoice_origin', '=', pos_order.name)])

                if installment and sent and int (saleAmount) != 0 and not discount :
                    tvc_dict = self._dict_invocie (tvc_invoice, int (saleAmount))
                    self.post_tvc_invoice (tvc_dict, tvc_invoice, False, int (saleAmount), item_card)
                    pos_order.is_tvc = True

                elif int(tvc_amount) == int (saleAmount):
                    pos_order.is_tvc = True
                else:

                    pos_order.is_installment = True
            else:
                product_item_code = ['AS-NG-G512LV-ES74', 'FX517ZM-AS73', 'FX517ZZR-F15-I73070', 'T3300KA-OLED001W',
                                     '90NB0S3B-M04830', 'TP412FA-4G003T', 'AN515-45-R0AX'
                    , 'AN515-45-R0AX/16G', 'G15-001-DGRY', 'LAT-3520-Ci5', 'LAT-5520-Ci7', 'CUS2130SH', '81WB00S0ED',
                                     '81WB0104ED', '53011WGC', '38M25AV', '204K7EA#ABV'
                    , '82JU00DYED', '82JU00E0ED', '82N600Q3ED', '81YT0000US', '82K800E3ED', '81YU0088ED', '82K200MHED',
                                     'GF65071', 'GF65092', 'GF65092/16G', 'GF63-11SC-224'
                    , 'GF63-11UC-262', '21O-00001', 'SM-S908EDRG/KSH', 'SM-S908EZGG/AN', 'SM-S908EZKG/KSH',
                                     'SM-S908EZKG/SH', 'SM-F711BZEEMEA/69', 'SM-F936BZK/BLK',
                                     'SM-F93BZE/BEIG', 'SM-F936BZA/GRY', 'P-27418978-S/HE', 'P27418872L',
                                     'P27418872L/SB', 'P27418948P', 'P27418948P/BL', ]
                item_code = False

                # offer_key = 'Offer11To13August2022'
                # offer_key = 'Offer20To22June2022'
                offer_key = 'Offer13october2022aljazeera'
                offer=True
                if pos_order.user_id.id == 110:
                    for payment in pos_order.statement_ids:

                        # if payment.journal_id.id ==  680:
                        if payment.journal_id.payment_type in ['cash','visa']:
                            # tvc_amount = ( payment.amount)
                            # saleAmount += tvc_amount
                            pass
                        else:
                            offer=False


                    tvc_invoice = self.env ['account.move'].search ([('invoice_origin', '=', pos_order.name)])

                    discount= False
                    if str(tvc_invoice.invoice_date) != '2022-07-20':
                        for order_line in pos_order.lines:

                            # if order_line.discount > 0 or order_line.product_id.sub_category_id.id in [71, 72] :
                            #     discount = True
                            if order_line.discount > 0  :
                                discount = True
                            if order_line.product_id.barcode in product_item_code:
                                item_code = True
                    # if  int (saleAmount) != 0 and not discount:
                    if  offer and not discount and item_code:


                        tvc_dict = self._dict_invocie_offer (tvc_invoice, int (-1*1000),offer_key)
                        self.post_tvc_invoice (tvc_dict, tvc_invoice, False, int (-1*1000), item_card)
                        pos_order.is_tvc = True

    def post_tvc_credit_invoice_point( self ,tvc_invoics_credits):

        for tvc_invoices_credit in tvc_invoics_credits:

            InvoiceAmount = 0

            item_card = ''

            if self._get_card(tvc_invoices_credit.partner_id,tvc_invoices_credit.invoice_date) :
                item_card = self._get_card(tvc_invoices_credit.partner_id,tvc_invoices_credit.invoice_date)
            for payment in tvc_invoices_credit.payment_move_line_ids :

                if payment.journal_id.payment_type == 'TVC' :
                    if payment.credit != 0 :
                        payment_amount = -payment.credit
                    elif payment.debit != 0 :
                        payment_amount = payment.debit

                    InvoiceAmount += payment_amount
            if int(InvoiceAmount)!=0:
                tvc_dict = self._dict_invocie(tvc_invoices_credit, int (InvoiceAmount))
                self.post_tvc_invoice(tvc_dict, tvc_invoices_credit,False, int (InvoiceAmount), item_card)
                # tvc_invoices_credit.partner_id.tvc_points += int ((InvoiceAmount) * 100)
    
    def post_so_tev_pay( self,tvc_sro_orders ):


        for tvc_sro_order in tvc_sro_orders :
            if tvc_sro_order.partner_id.national_id:
                saleAmount = 0

                if self._get_card (tvc_sro_order.partner_id,tvc_sro_order.create_date) :
                    item_card = self._get_card (tvc_sro_order.partner_id,tvc_sro_order.create_date)
                    card = True
                payments = self.env ['account.payment'].sudo ().search ([('order_id', '=', tvc_sro_order.id)])
                if payments :
                    for payment in payments :

                        if payment.journal_id.payment_type == 'TVC' :

                            tvc_amount = payment.amount
                            saleAmount += tvc_amount
                        if payment.journal_id.id == 826:
                            saleAmount = 2 * saleAmount
                    if saleAmount != 0 :
                        tvc_dict = self._dict_order (tvc_sro_order, int (-saleAmount))

                        self.post_tvc_invoice (tvc_dict,False, tvc_sro_order, int (-saleAmount), item_card)
                        # tvc_sro_order.partner_id.tvc_points += int ((InvoiceAmount) * 100)

    @api.model
    def sent_TVC_invoice ( self ) :
        # if self._cr.dbname == "tradelinestores-production-25284095" :

            invoice_date = datetime.today().date() - timedelta(days=30)
            invoice_date_today = datetime.today().date()
            if str (invoice_date) >= '2026-01-01' :
                tvc_invoices = self.sudo ().search (
                    [('invoice_date', '=', invoice_date), ('move_type', '=', 'out_invoice'), ('payment_state', 'in', ['not_paid','paid','in_payment','partial','reversed']),
                     ('is_tvc', '=', False),
                    ], order='invoice_date')
                self.post_invoiced (tvc_invoices)
                tvc_sro_orders = self.sudo ().env ['sale.order'].search (
                    ['|', ('date_order', '=', invoice_date),('date_order', '=', invoice_date), ('inv_type', '=', 'sro'), ('state', '=', 'sale'),
                     ('is_tvc', '=', False),
                    ], order='date_order')
                self.post_so (tvc_sro_orders)


    @api.model
    def sent_TVC_credit( self ):

        if self._cr.dbname == "tradelinestores-production-25284095" :


            tvc_credits = self.sudo().search([('invoice_date','>=','2026-1-1'),('type','=','out_refund'),('state','=','paid'),('is_tvc','=',False),
                                              ('is_installment','=', False), '|' ,('partner_id.customer_type', '=', 'Individual'),('partner_id.foreigner_type', '=', 'Person'),('partner_id.is_exempt_select', 'not in', ['true']),('team_id', '!=', 52)],order='invoice_date')
            self.post_credit(tvc_credits)



    # @api.model
    # def sent_tvc_point( self ):
    #     # if self._cr.dbname == "tradelinestores-production-25284095" :
    #     if self._cr.dbname == "tradelinestores-production-25284095" :
    #
    #         tvc_bank_pays = self.env['account.bank.statement.line'].sudo ().search (
    #             [ ('date', '>=', '2026-01-01'),('journal_id.payment_type', '=', 'TVC'), ('is_point', '=', False)
    #              ], order='date')
    #         self.post_tvc_point (tvc_bank_pays)
    #         tvc_invoices = self.sudo ().search (
    #             [('invoice_date', '>=', '2026-1-01'), ('state', '=', 'paid'),
    #              ('is_point', '=', False), ('payment_journal', 'ilike', 'TVC'),
    #              ('team_id', '!=', 52)], order='invoice_date')
    #         self.post_tvc_credit_invoice_point (tvc_invoices)
    #         tvc_sro_orders_pay = self.sudo ().env ['sale.order'].search (
    #             [('quotation_type', '=', 'sro'), ('state', '=', 'sale'), ('create_date', '>=', '2026-1-1'),
    #              ('is_point', '=', False), ('payment_journal_text', 'ilike', 'TVC'),
    #              ('team_id', '!=', 52)], order='create_date')
    #
    #         self.post_so_tev_pay (tvc_sro_orders_pay)







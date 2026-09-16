# -*- coding: utf-8 -*-
import json
import logging
import urllib
from datetime import datetime
import hashlib

import pytz
import requests
import base64
from odoo import models, fields, api, _
from odoo.exceptions import UserError
from odoo.exceptions import ValidationError
import logging
from datetime import datetime, time


_logger = logging.getLogger(__name__)
grey = "\x1b[38;21m"
yellow = "\x1b[33;21m"
red = "\x1b[31;21m"
bold_red = "\x1b[31;1m"
reset = "\x1b[0m"
green = "\x1b[32m"
blue = "\x1b[34m"


def fromObjectTo(old_value, new_str):
    for old_key in old_value:
        new_value = old_value[old_key]
        if type(new_value) is dict:
            new_new_key = old_key.upper()
            new_str += '"' + new_new_key + '"'
            for the_value in new_value:
                new_new_valu = new_value[the_value]
                new_new_new_key = the_value.upper()
                new_str += '"' + new_new_new_key + '"'
                if type(new_new_valu) is dict:
                    for pre_value in new_new_valu:
                        pre_new_valu = new_new_valu[pre_value]
                        pre_new_new_key = pre_value.upper()
                        new_str += '"' + pre_new_new_key + '"'
                        new_value_s = str(pre_new_valu)
                        new_str += '"' + new_value_s + '"'
                elif type(new_new_valu) is list:
                    new_new_new_key = the_value.upper()
                    new_str += '"' + new_new_new_key + '"'
                    fromObjectTo(new_new_valu, new_str)
                else:
                    new_value_s = str(new_new_valu)
                    new_str += '"' + new_value_s + '"'
        elif type(new_value) is list:
            for new_list_value in new_value:
                pre_key = old_key.upper()
                new_str += '"' + pre_key + '"'
                for the_value in new_list_value:
                    new_new_valu = new_list_value[the_value]
                    new_new_new_key = the_value.upper()
                    new_str += '"' + new_new_new_key + '"'
                    if type(new_new_valu) is dict:
                        for pre_value in new_new_valu:
                            pre_new_valu = new_new_valu[pre_value]
                            pre_new_new_key = pre_value.upper()
                            new_str += '"' + pre_new_new_key + '"'
                            new_value_s = str(pre_new_valu)
                            new_str += '"' + new_value_s + '"'
                    elif type(new_new_valu) is list:
                        new_new_new_key = the_value.upper()
                        new_str += '"' + new_new_new_key + '"'
                        fromObjectTo(new_new_valu, new_str)
                    else:
                        new_value_s = str(new_new_valu)
                        new_str += '"' + new_value_s + '"'
        else:
            new_new_key = old_key.upper()
            new_str += '"' + new_new_key + '"'
            new_new_value = str(new_value)
            new_str += '"' + new_new_value + '"'
    return new_str


def fromObjecttoUpperCaseString(document):
    new_str = ''
    for key in document:
        value = document[key]
        if type(value) is dict:
            new_key = key.upper()
            new_str += '"' + new_key + '"'
            new_str = fromObjectTo(value, new_str)
        elif type(value) is list:
            pre_key = key.upper()
            new_str += '"' + pre_key + '"'
            for new_list_value in value:
                pre_key = key.upper()
                new_str += '"' + pre_key + '"'
                for the_value in new_list_value:
                    new_new_valu = new_list_value[the_value]
                    new_new_new_key = the_value.upper()
                    new_str += '"' + new_new_new_key + '"'
                    if type(new_new_valu) is dict:
                        for pre_value in new_new_valu:
                            pre_new_valu = new_new_valu[pre_value]
                            pre_new_new_key = pre_value.upper()
                            new_str += '"' + pre_new_new_key + '"'
                            new_value = str(pre_new_valu)
                            new_str += '"' + new_value + '"'
                    elif type(new_new_valu) is list:
                        for ore_new_new_valu in new_new_valu:
                            new_new_new_key = the_value.upper()
                            new_str += '"' + new_new_new_key + '"'
                            new_str = fromObjectTo(ore_new_new_valu, new_str)
                    else:
                        new_value = str(new_new_valu)
                        new_str += '"' + new_value + '"'
        else:
            new_key = key.upper()
            new_str += '"' + new_key + '"'
            new_value = str(value)
            new_str += '"' + new_value + '"'
    return new_str


class POSOrder(models.Model):
    _inherit = 'pos.order'

    @api.onchange('electronic_invoice_uuid', 'electronic_invoice_status')
    def _check_invoice_status(self):
        for invoice in self:
            e_invoice_invalid = e_invoice_valid = hide_sent_button = e_invoice_sent = e_invoice_submitted = False
            if invoice.electronic_invoice_uuid:
                if invoice.electronic_invoice_status == 'draft':
                    e_invoice_sent = True
                elif invoice.electronic_invoice_status == 'Submitted':
                    e_invoice_submitted = True
                    hide_sent_button = True
                elif invoice.electronic_invoice_status == 'Valid':
                    e_invoice_valid = True
                    hide_sent_button = True
                elif invoice.electronic_invoice_status == 'Invalid':
                    e_invoice_invalid = True
            invoice.e_invoice_submitted = e_invoice_submitted
            invoice.e_invoice_valid = e_invoice_valid
            invoice.hide_sent_button = hide_sent_button
            invoice.e_invoice_sent = e_invoice_sent
            invoice.e_invoice_invalid = e_invoice_invalid

    @api.model
    def create_from_ui(self, orders, draft=False):
        """ Create and update Orders from the frontend PoS application.

        Create new orders and update orders that are in draft status. If an order already exists with a status
        diferent from 'draft'it will be discareded, otherwise it will be saved to the database. If saved with
        'draft' status the order can be overwritten later by this function.

        :param orders: dictionary with the orders to be created.
        :type orders: dict.
        :param draft: Indicate if the orders are ment to be finalised or temporarily saved.
        :type draft: bool.
        :Returns: list -- list of db-ids for the created and updated orders.
        """
        order_ids = []
        for order in orders:
            existing_order = False
            if 'server_id' in order['data']:
                existing_order = self.env['pos.order'].search(
                    ['|', ('id', '=', order['data']['server_id']), ('pos_reference', '=', order['data']['name'])],
                    limit=1)
            if (existing_order and existing_order.state == 'draft') or not existing_order:
                order_ids.append(self._process_order(order, draft, existing_order))

        return self.env['pos.order'].search_read(domain=[('id', 'in', order_ids)],
                                                 fields=['id', 'pos_reference', 'electronic_invoice_url'])

    hide_sent_button = fields.Boolean(compute=_check_invoice_status)
    e_invoice_valid = fields.Boolean(compute=_check_invoice_status)
    e_invoice_invalid = fields.Boolean(compute=_check_invoice_status)
    e_invoice_submitted = fields.Boolean(compute=_check_invoice_status)
    e_invoice_sent = fields.Boolean("E-Invoice Informed", copy=False,
                                    help="If checked so the Electronic Invoice is sent")
    e_invoice_json = fields.Text("E_invoice JSON", copy=False)
    e_invoice_canonical = fields.Text("E_invoice Canonical", copy=False)
    electronic_invoice_uuid = fields.Char("E-Invoice ID", readonly=True, copy=False)
    electronic_invoice_url = fields.Char(compute="_compute_e_invoice_url", string="Electronic Invoice", copy=False)
    electronic_invoice_file = fields.Many2one("ir.attachment", "E-Invoice PDF", copy=False)
    electronic_invoice_pdf = fields.Binary("E-Invoice PDF", copy=False)
    pdf_name = fields.Char('File Name', copy=False)
    show_results = fields.Boolean('Show Results', copy=False, default=True)
    electronic_invoice_date = fields.Datetime("E-Invoice Date", help="Date of sending E-Invoice,"
                                                                     " used in validate 72H in case of cancel invoice")
    electronic_invoice_status = fields.Char("E-Invoice Status", help="Status on invoice in Taxes system",
                                            default='Draft')
    uuid = fields.Char(string="UUID", required=False, )
    old_uuid = fields.Char(string="Old UUID", required=False,compute='_compute_func_old_uuid' )
    previous_uuid = fields.Char(string="Previous UUID", required=False, )
    ref_old_uuid = fields.Char(string="referenceOldUUID", required=False, )
    result = fields.Text(string="", required=False, )
    long_id = fields.Char(string="", required=False, )


    @api.depends('lines.refund_orderline_ids', 'lines.refunded_orderline_id')
    def _compute_func_old_uuid(self):
       for rec in self:
           if rec.mapped('lines.refund_orderline_ids.order_id'):
               rec.old_uuid=rec.mapped('lines.refund_orderline_ids.order_id').uuid
           else:
               rec.old_uuid=False

    def get_idSrv_url(self):
        configType = self.config_id.l10n_eg_production_env
        if not configType:
            return "https://id.preprod.eta.gov.eg"
        else:
            return "https://id.eta.gov.eg"

    def get_clientId_Secret(self):
        user = self.config_id.l10n_eg_client_identifier
        secret = self.config_id.l10n_eg_client_secret
        return user, secret

    def get_base_url(self):
        configType = self.config_id.l10n_eg_production_env
        if not configType:
            return "https://api.preprod.invoicing.eta.gov.eg"
        else:
            return "https://api.invoicing.eta.gov.eg"

    def get_access_token(self):
        idSrv = self.get_idSrv_url()
        urlapi=self.get_base_url()

        url = idSrv + "/connect/token"

        clientId, clientSecret = self.get_clientId_Secret()
        headers = {'content-type': "application/x-www-form-urlencoded", 'cache-control': "no-cache",
                   'posserial': self.config_id.deviceSerialNumber,
                   'pososversion': 'Windows', 'presharedkey': ''
                   }
        header = json.dumps(headers, indent=4, ensure_ascii=False).encode('utf8')

        payload = {
            'grant_type': 'client_credentials',
            'client_id': clientId,
            'client_secret': clientSecret,
        }
        details = json.dumps(payload, indent=4, ensure_ascii=False).encode('utf8')
        response = requests.post(url=url, headers=headers, data=payload)
        if response.json().get('error'):

            raise ValidationError(_(response.json().get('error')))
        _logger.warning(response.text)

        try:
            return response.json()['access_token'], clientId, clientSecret, urlapi
        except:
            return False

    @api.onchange('electronic_invoice_uuid')
    def _compute_e_invoice_url(self):
        """
		Used to generate url for the invoice printed label
		:return:
		"""
        for rec in self:
            electronic_invoice_url = False
            if rec.uuid:
                invoice_time = datetime.combine(self.date_order, datetime.min.time())
                invoice_time = invoice_time.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

                electronic_invoice_url = self.get_base_url() + "/receipts/search/%s/share/%s" % (
                    rec.uuid, invoice_time)
            rec.electronic_invoice_url = electronic_invoice_url

    def action_send_electronic_invoice(self):
        message = ""
        result_lines = []
        for invoice in self:
            # Generate new token
            access_token, client_id, client_secret, apiBaseUrl = self.get_access_token()
            if access_token:
                # Assign Headers
                headers = {'Content-Type': "application/json", 'cache-control': "no-cache",
                           'Accept': "application/json", "Accept-Language": "ar",
                           'Authorization': "Bearer %s" % access_token}
                if not invoice.e_invoice_json:
                    # Generate JSON
                    invoice.action_generate_json()
                inv_params = eval(invoice.e_invoice_json)
                req_body = {'receipts': [inv_params]}
                submit_url = apiBaseUrl + "/api/v1/receiptsubmissions"
                details = json.dumps(req_body, indent=4, ensure_ascii=False).encode('utf8')
                try:
                    response = requests.post(url=submit_url, headers=headers, data=details, verify=False)
                except Exception as e:
                    message = "Couldn't Connect to %s due to connection error:\n %s" % (submit_url, e)
                    logging.info(red + message + reset)
                    # raise UserError(message)
                if response.status_code == 404:
                    message = "Internal error code:1001" \
                              "\n Connecting Egyptian taxes API to submit document respond with error code: [%s]" % response.status_code
                    message += "\n\nError Desc.: %s" % response.reason
                    logging.info(red + message + reset)
                elif response.status_code in (200, 202):
                    result = response.json()
                    logging.info(green + "response: %s" % response + reset)
                    logging.info(green + "Result: %s" % result + reset)
                    # Extract Results:
                    if result.get('acceptedDocuments'):
                        message += "<h4 style='color:green'>Success submit of %s Document <h4><br/>" % len(
                            result['acceptedDocuments'])
                        for accept_detail in result['acceptedDocuments']:
                            document_status = "Submitted"
                            logging.info(yellow + "UUID: %s" % accept_detail['longId'] + reset)
                            # TODO:: This option to auto update is stopped because of 404 that is from non saved uuid on taxes yet
                            invoice.write({'electronic_invoice_uuid': result.get('submissionUUID') or result.get('submissionId'),
                                           'long_id':accept_detail.get('longId'),
                                           'e_invoice_sent': True,
                                           'electronic_invoice_status': 'Submitted',
                                           'electronic_invoice_date': fields.Datetime.now()})
                    if result.get('rejectedDocuments'):
                        message += "Errors on submit of %s Document" % len(
                            result['rejectedDocuments'])
                        

                        logging.info(red + "\n Invoice %s Rejected" % invoice.name + reset)
                else:
                    result = response.json()
                    message = "Internal error code:1002" \
                              "\n Connecting Egyptian taxes API to submit document respond with error code: [%s]" % response.status_code
                    message += "\n\nError Desc.: %s" % response.reason
                    message += "\n\nresult: %s" % result
                self.result = response.text

        return True

    def action_update_electronic_invoice_status(self):
        for invoice in self:
            result = invoice._action_get_document('raw')
            invoice.write({'electronic_invoice_status':result.get('receipt').get('status')})
            return True

    def action_update_electronic_invoice_pdf(self):
        for invoice in self:
            result = invoice._action_get_document('pdf')
            attachment = invoice._create_attachment('Tax-ETA', result)
            invoice.electronic_invoice_file = attachment
            invoice.electronic_invoice_pdf = attachment.datas
            invoice.pdf_name = attachment.name

    def _create_attachment(self, name, datas):
        return self.env['ir.attachment'].sudo().create({
            'name': ("%s-%s" % (self.name, name)).replace('/', '_'),
            'datas': base64.b64encode(datas),
            'mimetype': 'application/x-pdf',
            'type': 'binary',
            'res_model': self._name,
            'res_id': self.id
        })

    def _get_personal_details(self, partner):
        if partner.commercial_partner_id.country_code == 'EG':
            type = 'B' if partner.commercial_partner_id.is_company else 'P'
        elif partner:
            type = 'F'
        else:
            type = 'P'
        id = partner.vat or '',
        name = partner.name or '',
        mobileNumber = partner.phone or '',

        data = {
            "type": type,
            "id": id[0],
            "name": name[0],
            "mobileNumber": mobileNumber[0],

        }
        return data

    def _get_invoice_lines(self):
        invoice_lines = []
        total_discount = 0.00000
        total_sales_amount = 0.00000
        EGP = self.env.ref('base.EGP')
        for line in self.lines.filtered(lambda l:l.qty):
            amountEGP = line.price_unit if line.price_unit else 1.00
            currencySold = self.currency_id.name
            amountSold = 0.00
            currencyExchangeRate = 0.00
            total = line.price_subtotal_incl
            if line.currency_id != EGP:
                # TODO: NEEDED to be changed
                amountEGP = line.currency_id.compute(line.price_unit, EGP)
                amountSold = line.price_unit if line.price_unit else 1.00
                currencySold = line.currency_id.name
                currencyExchangeRate = EGP.rate
                total = line.currency_id.compute(line.price_subtotal_incl, EGP)
            price_unit_wo_discount = line.price_unit * (1 - (line.discount / 100.0))
            discount_percentage = line.discount if line.discount else 0.00000
            quantity = line.qty if line.qty else 1.0
            sales_total_amount = amountEGP * quantity

            discount_amount = (discount_percentage / 100) * sales_total_amount

            total_discount += discount_amount
            taxes_res = line.tax_ids._origin.compute_all(price_unit_wo_discount,
                                                         quantity=line.qty, currency=line.currency_id,
                                                         product=line.product_id,
                                                         )
            total_sales_amount += sales_total_amount
            unitType = line.product_uom_id.l10n_eg_unit_code_id.code
            netTotal = sales_total_amount - discount_amount
            item_code = line.product_id.l10n_eg_eta_code or line.product_id.barcode or '99999999'
            itemType = item_code.startswith('EG') and 'EGS' or 'GS1'
            taxableItems = line._get_taxableItems(taxes_res['taxes'])
            if line.discount:
                    invoice_lines.append({
                        "internalCode": line.product_id.default_code or "Code",
                        "description": line.product_id.name or 'test',
                        "itemType": itemType,
                        "itemCode": item_code,
                        "unitType": abs(unitType),
                        "quantity": round(abs(quantity), 5),
                        "unitPrice": round(abs(amountEGP), 5),
                        "commercialDiscountData":[{
                            'description':str(line.discount),
                            'amount':round(abs(discount_amount),5)
                        }],
                        "netSale": round(abs(netTotal), 5),
                        "totalSale": round(abs(sales_total_amount), 5),
                        "total": round(abs(total), 5),
                        "taxableItems": taxableItems
                    }
                    )
            else:
                invoice_lines.append({
                    "internalCode": line.product_id.default_code or "Code",
                    "description": line.product_id.name or 'test',
                    "itemType": itemType,
                    "itemCode": item_code,
                    "unitType": unitType,
                    "quantity": round(abs(quantity), 5),
                    "unitPrice": round(abs(amountEGP), 5),
                    "netSale": round(abs(netTotal), 5),
                    "totalSale": round(abs(sales_total_amount), 5),
                    "total": round(abs(total), 5),
                    "taxableItems": taxableItems
                }
                )
        return invoice_lines, total_discount, total_sales_amount

    def _action_get_document(self, endpoint):
        access_token, client_id, client_secret, apiBaseUrl = self.get_access_token()
        headers = {'Content-Type': "application/json", 'cache-control': "no-cache",
                   'Accept': "application/json", "Accept-Language": "ar",
                   'Authorization': "Bearer %s" % access_token}
        get_details_url = apiBaseUrl + "/api/v1/receipts/%s/%s" % (self.uuid,endpoint)
        logging.info(yellow + "get_details_url: %s" % get_details_url + reset)
        try:
            response = requests.get(url=get_details_url, headers=headers, verify=False)
        except Exception as e:
            message = "Couldn't Connect to %s due to connection error:\n %s" % (get_details_url, e)
            logging.info(red + message + reset)
            raise UserError(message)
        logging.info(green + "response: %s" % response + reset)
        if response.status_code == 404:
            message = "Connecting Egyptian taxes API to get document respond with error code: [%s]" % response.status_code
            message += "\n\nURL.: %s" % get_details_url
            message += "\n\nError Desc.: %s" % response.reason
            if endpoint == 'raw':
                self.write({'electronic_invoice_status':'Invalid'})
            raise UserError(_(message))
        elif response.status_code in (200, 202):
            result = response
            if endpoint == 'raw':
                result = response.json()
                logging.info(green + "Result: %s" % result + reset)
            elif endpoint == 'pdf':
                result = response.content
            return result
        else:
            result = response.json()
            message = "Connecting Egyptian taxes API to Cancel document respond with error code: [%s]" % response.status_code
            message += "\n\nError Desc.: %s" % response.reason
            message += "\n\nURL: %s" % get_details_url
            message += "\n\nDetails: %s" % result
            raise UserError(_(message))

    def get_issuer(self):
        data = {
            "rin": self.company_id.company_registry or self.company_id.vat,
            "companyTradeName": self.company_id.name,
            "branchCode": self.config_id.branch or "0",
            "branchAddress": {
                "country": self.company_id.country_id.code,
                "governate": self.company_id.state_id.name or '',
                "regionCity": self.company_id.city or '',
                "street": self.company_id.street or '',
                "buildingNumber": self.company_id.partner_id.l10n_eg_building_no or '',

            },
            "deviceSerialNumber": self.config_id.deviceSerialNumber,
            "activityCode": self.config_id.activityCode
        }
        return data

    def action_generate_json(self):
        invoice_lines, totalDiscountAmount, totalSalesAmount = self._get_invoice_lines()
        netAmount = (totalSalesAmount - totalDiscountAmount)
        EGP = self.env.ref('base.EGP')
        if self.currency_id != EGP:

            totalAmount = self.currency_id.compute(self.amount_total, EGP)
        else:
            totalAmount = self.amount_total
        invoice_time = self.date_order.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        # taxpayerActivityCode = self.company_id.taxpayer_activity_code or "1061"

        receiptType = 'S' if not self.mapped('lines.refund_orderline_ids.order_id') else 'R'

        issuer = self.get_issuer()
        receiver = self._get_personal_details(self.partner_id)
        invoice_params = {
            "header": {
                "dateTimeIssued": invoice_time,
                "receiptNumber": self.name,
                "uuid": '',
                "previousUUID": self.previous_uuid or '',
                "currency": self.currency_id.name,
                "exchangeRate": 0,
                "orderdeliveryMode": "FC",
            },
            "documentType": {
                "receiptType": receiptType,
                "typeVersion": "1.2"
            },
            "seller": issuer,
            "buyer": receiver,
            "itemData": invoice_lines,
            "totalSales": round(abs(totalSalesAmount),5),
            "totalItemsDiscount": 0.0,
            "totalCommercialDiscount": round(abs(totalDiscountAmount),5),
            "taxTotals": self.get_total_tax(),
            "netAmount": round(abs(netAmount),5),
            "totalAmount": round(abs(totalAmount),5),
            "paymentMethod": 'C',
        }
        if receiptType=='R':
            invoice_params['header'].update({'referenceUUID': self.old_uuid})
        if self.ref_old_uuid:
            invoice_params['header'].update({'referenceOldUUID': self.ref_old_uuid})
        new_str = fromObjecttoUpperCaseString(invoice_params)
        self.uuid = hashlib.sha256(str(new_str).encode()).hexdigest()
        invoice_params['header'].update({'uuid': self.uuid})
        self.e_invoice_json = str(invoice_params).encode('utf-8')
        self.e_invoice_canonical = str(new_str).encode('utf-8')
        return invoice_params

    def get_total_tax(self):
        total_tax = []
        tax_type=self.lines.tax_ids.mapped('l10n_eg_eta_code')
        for tax in tax_type:
            total_tax.append({'taxType':tax.split('_')[0].upper().upper(),'amount':0.0})

        for line in self.lines:
            price_unit_wo_discount = line.price_unit * (1 - (line.discount / 100.0))
            taxes_res = line.tax_ids._origin.compute_all(price_unit_wo_discount,
                                                         quantity=line.qty, currency=line.currency_id,
                                                         product=line.product_id,
                                                         )

            taxableItems = line._get_taxableItems(taxes_res['taxes'])

            for tax in taxableItems:
                key='taxType'
                val=tax.get('taxType')
                d = next(filter(lambda d: d.get(key) == val, total_tax), None)
                if d:
                    d.update({'amount':round(d.get('amount')+tax.get('amount'),5)})

        return total_tax

    @api.model_create_multi
    def create(self, vals):

        new_record = super(POSOrder, self).create(vals)
        order = self.env['pos.order'].search(
            [('session_id.config_id', '=', new_record.session_id.config_id.id), ('id', '!=', new_record.id)], order='date_order desc',
            limit=1)
        new_record.name = new_record._compute_order_name()
        new_record.previous_uuid = order.uuid
        new_record.action_generate_json()
        return new_record

    def auto_send(self):
        start=datetime.combine(datetime.now(), time.min)
        end=datetime.combine(datetime.now(), time.max)

        orders=self.env['pos.order'].search([('session_id.config_id.deviceSerialNumber','!=',False),('e_invoice_sent','=',False),
                                             ('date_order','>=',start),('date_order','<',end)])
        for order in orders:
            order.action_send_electronic_invoice()






class POSORDERLINE(models.Model):
    _inherit = 'pos.order.line'

    def _get_taxableItems(self, taxes_res):
        """
		Compute taxable lines
		:param taxes_res:
		:return: taxableItems
		"""
        tax_obj = self.env['account.tax']
        taxableItems = []
        EGP = self.env.ref('base.EGP')
        if taxes_res:
            for tax_line in taxes_res:
                tax = tax_obj.browse(tax_line['id'])
                amount = abs(tax_line['amount'])
                # if self.currency_id:
                #     # TODO: NEEDED to be changed
                #     amount = self.currency_id.compute(amount, EGP)
                taxType = tax.l10n_eg_eta_code.split('_')[0].upper().upper()
                subType = tax.l10n_eg_eta_code.split('_')[1].upper()
                rate = abs(tax.amount)
                taxableItems.append({
                    "taxType": taxType,
                    "amount": round(abs(amount), 5),
                    "subType": subType,
                    "rate": round(rate, 5)
                })

        return taxableItems

from odoo import fields, models, api
import json
from odoo.exceptions import UserError
import requests
import logging
_logger = logging.getLogger(__name__)
from base64 import b64encode
class BaseIntegration(models.Model):
    _name = 'base.integration'
    _description = 'Base Integration'

    name = fields.Char(string="Mall Name")
    user = fields.Char(string="UserName")
    password = fields.Char(string="password")
    store_id = fields.Char(string="Store ID")
    x_api_key = fields.Char(string="x-api-key")
    token = fields.Char(string="Token")
    base_url = fields.Char(string="Base Url")
    end_point = fields.Char(string="End Point")
    authorization = fields.Char(string="Authorization")
    content_type = fields.Char(string="Content-Type")
    branch = fields.Many2one(
        comodel_name='res.branch',
        string='Branch',
        required=True)

    store_key = fields.Selection(
        string='Store Key',
        selection=[('store', 'Store'),
                   ('store_id', 'Store ID'), ],
        required=True, )



    def get_token(self):


        endpoint = "/Users/Login"
        url = self.base_url + endpoint
        headers = {
            "Accept" : "application/json",
            "Content-Type" :self.content_type,
            "x-api-key" : self.x_api_key,
            "Authorization":"Basic "+self.authorization,
            "Accept-Encoding":"gzip, deflate, br",
            "Connection":"keep-alive"
        }
        data = {
            'userName' :self.user,
            'password':self.password
        }
        data = json.dumps(data)

        try:
            response = requests.post(url=url, headers=headers, data=data,verify=False)
            if response.status_code == 200:
                self.token = response.json()['result']['token']
            else:
                raise UserError(response)

        except Exception as e:
            pass
    def get_token_auto(self):

        endpoint = "/Users/Login"
        url = self.base_url + endpoint
        headers = {
            "Accept" : "application/json",
            "Content-Type" : self.content_type,
            "x-api-key" : self.x_api_key,
            "Authorization": "Basic " + self.authorization,
            "Accept-Encoding":"gzip, deflate, br",
            "Connection":"keep-alive"
        }
        data = {
            'userName' : self.user,
            'password': self.password
        }
        data = json.dumps(data)
        try:
            response = requests.post(url=url, headers=headers, data=data,verify=False)
            if response.status_code == 200:
                token = response.json()['result']['token']
                self.token =token
                return token
            else:
                raise UserError(response)

        except Exception as e:
            pass

    def _prepare_invoices(self, days_line_of_store):
        for line in days_line_of_store:
            mallsTransactionsItems_dict = {}
            mallsTransactionsItems_dict.setdefault(str(self.store_key),self.store_id)
            mallsTransactionsItems_dict.setdefault('invoice_no', line.invoices_number)
            mallsTransactionsItems_dict.setdefault('invoice_date', line.config_day_id.date.strftime('%Y-%m-%d 00:00:00'))
            mallsTransactionsItems_dict.setdefault('subtotal',str( round(line.new_untaxed_amount, 2)))
            mallsTransactionsItems_dict.setdefault('tax', str(round(line.new_tax_amount, 2)))
            mallsTransactionsItems_dict.setdefault('service', "00.00")
            mallsTransactionsItems_dict.setdefault('total', str(round(line.new_total_amount, 2)))
            mallsTransactionsItems_dict.setdefault('discount', "0")
            self.post_store(mallsTransactionsItems_dict)

    def post_store(self,invoices):

        # endpoint = "//Transactions/Record"
        url = self.base_url +self.end_point
        token = self.get_token_auto()
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-api-key": self.x_api_key,
            "x-token": token,
            "Authorization": "Basic "+self.authorization,
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive"
        }

        data = json.dumps(invoices)

        try:
            response = requests.post(url=url, headers=headers, data=data,verify=False )
            _logger.info("Odoo response %s", response)

            if response.status_code == 200:
              pass
            else:
                message = response
                raise UserError(message.text)


        except Exception as e:
           pass

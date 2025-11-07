from odoo import fields, models, api,_
from odoo.exceptions import ValidationError

import base64
from tempfile import TemporaryFile
import openpyxl
class TVCSetting (models.Model) :
    _name = 'tvc.setting'

    from_date = fields.Date(
        string='From Date',
        required=False)
    to_date = fields.Date(
        string='TO Date',
        required=False)
    offer_key = fields.Char(
        string='Offer key',
        required=False)

    invoice_id = fields.Many2one(
        comodel_name='account.move',
        string='Invoice',
        required=False)
    order_id = fields.Many2one(
        comodel_name='sale.order',
        string='Order',
        required=False)
    offer = fields.Boolean(
        string='Offer',
        required=False)
    amount = fields.Integer(
        string='Amount',
        required=False)


    exel_Sheet = fields.Binary (string="Excel Sheet",required=False )

    def import_excel ( self ) :
        # Generating of the excel file to be read by openpyxl
        if self.exel_Sheet:
            file = base64.decodestring (self.exel_Sheet)
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
                invoice_number = row [2].value
                invoice_amount = row [5].value
                invoice_point = row [6].value
                invoice = self.env['account.move'].search([('name','=',invoice_number)])
                if invoice != None and index !=0 :
                    self.post_file_point(invoice,invoice_amount,invoice_point)
        else:
            raise ValidationError (
                _ ("Upload Sheet"))


    def sent_TVC_invoice ( self ) :
        if self._cr.dbname == "live_11nov_2024" :
            if self.from_date and self.to_date:
                if self.offer:
                    tvc_invoices = self.sudo ().env ['account.move'].invoice_date (
                        [('invoice_date', '>=', self.from_date),('date_invoice', '<=', self.to_date),
                         ('type', '=', 'out_invoice'),
                         ('state', 'in', ['paid', 'open']),('sms_sent','=',True),
                         ('is_tvc', '=', False),('residual_signed', '<=', 1),
                         '|',('partner_id.customer_type', '=', 'Individual'),
                         ('partner_id.foreigner_type', '=', 'Person'), ('team_id', '!=', 52),('offer','=',True),], order='date_invoice')
                    self.env ['account.invoice'].post_invoiced_offer (tvc_invoices,self.offer_key)
                    # tvc_sro_orders = self.sudo ().env ['sale.order'].search (
                    #     [('create_date', '=', self.from_date),
                    #      ('quotation_type', '=', 'sro'), ('state', '=', 'sale'),
                    #      ('is_tvc', '=', False), ('user_id.name', 'ilike', 'TLS'),('offer','=',True),
                    #      '|', ('partner_id.customer_type', '=', 'Individual'),
                    #      ('partner_id.foreigner_type', '=', 'Person'), ('team_id', '!=', 52),('partner_id.is_exempt_select', 'not in', ['true'])], order='create_date')
                    # self.env ['account.invoice'].post_so_offer (tvc_sro_orders,self.offer_key)
                elif str (self.from_date) >= '2022-01-01' :
                    tvc_invoices = self.sudo ().env ['account.move'].search (
                        [('invoice_date', '>=', self.from_date),('date_invoice', '<=', self.to_date), ('type', '=', 'out_invoice'),
                         ('state', 'in', ['paid', 'open']),
                         ('is_tvc', '=', False), ('residual_signed','<=',1),('offer','=',False),
                          '|',('partner_id.company_type', '=', 'person'),('team_id', '!=', 52)], order='invoice_date')
                    self.env ['account.move'].post_invoiced(tvc_invoices)
                    tvc_sro_orders = self.sudo ().env ['sale.order'].search (
                        [('date_order', '>=', self.from_date), ('date_order', '<=', self.to_date), ('quotation_type', '=', 'sro'), ('state', '=', 'sale'),
                         ('is_tvc', '=', False),('offer','=',False),
                         '|',('partner_id.company_type', '=', 'person') ,('team_id', '!=', 52)], order='invoice_date')
                    self.env ['account.move'].post_so (tvc_sro_orders)
            else:
                if self.offer:
                    if self.invoice_id:
                        if self.amount:

                            if self.invoice_id.type=='out_invoice':
                                item_card = self.env ['account.move']._get_card (self.invoice_id.partner_id, self.invoice_id.date_invoice)

                                tvc_dict = self.env ['account.move']._dict_invocie_offer (self.invoice_id, int ( self.amount),self.offer_key)
                                self.env ['account.move']._post_tcv_invoice (tvc_dict, self.invoice_id, False, int ( self.amount), item_card)
                            else:
                                item_card = self.env ['account.move']._get_card (self.invoice_id.partner_id,
                                                                                    self.invoice_id.date_invoice)

                                tvc_dict = self.env ['account.move']._dict_invocie_offer (self.invoice_id,
                                                                                             -1*int (self.amount),
                                                                                             self.offer_key)
                                self.env ['account.move']._post_tcv_invoice (tvc_dict, self.invoice_id, False,
                                                                                -1*int (self.amount), item_card)
                        else:
                            tvc_invoices = self.sudo ().env ['account.move'].search (
                                [('id', '=', self.invoice_id.id),])
                            if tvc_invoices.type=='out_invoice':
                                self.env ['account.move'].post_invoiced_offer (tvc_invoices,self.offer_key)
                            else:
                                self.env ['account.move'].post_invoiced_credit_offer (tvc_invoices, self.offer_key)
                    elif self.order_id:
                        if self.amount :
                            item_card = self._get_card (self.order_id.partner_id, self.order_id.create_date)

                            tvc_dict = self._dict_order_offer (self.order_id, int (self.amount ),self.offer_key)

                            self._post_tcv_invoice (tvc_dict, False, self.order_id, int (self.amount), item_card)

                        else:
                            tvc_order = self.sudo ().env ['sale.order'].search (
                                [('id', '=', self.order_id.id)])
                            self.env ['account.move'].post_so_offer (tvc_order,self.offer_key)
                else:
                    tvc_invoices = self.sudo ().env ['account.move'].search (
                        [('id', '=', self.invoice_id.id)])
                    self.env ['account.move'].post_invoiced (tvc_invoices)
                    tvc_order = self.sudo ().env ['sale.order'].search (
                        [('id', '=', self.order_id.id)])
                    self.env ['account.move'].post_so (tvc_order)


    def post_file_point( self ,invoice,InvoiceAmount,point):
            item_card = ''

            if invoice._get_card(invoice.partner_id,invoice.date_invoice) :
                item_card = invoice._get_card(invoice.partner_id,invoice.date_invoice)
            if int(InvoiceAmount)!=0:
                tvc_dict = invoice._dict_invocie(invoice, int (InvoiceAmount))
                invoice.post_tvc_invoice(tvc_dict, invoice,False, int (InvoiceAmount), item_card)

            invoice.send_smss(invoice.partner_id.mobile,point)

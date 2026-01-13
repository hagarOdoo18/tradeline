# -*- coding: utf-8 -*-
import logging

import requests
from odoo import models, fields, _
from odoo.exceptions import UserError

DOC_TYPES = [('I', "Invoice"), ('C', "Credit Note"), ('D', "Debit Note")]
igrey = '\x1b[38;21m'
yellow = '\x1b[33;21m'
red = '\x1b[31;21m'
bold_red = '\x1b[31;1m'
reset = '\x1b[0m'
green = '\x1b[32m'
blue = '\x1b[34m'
# Ahmed Salama Code Start ---->


class VendorReceivedDocument(models.Model):
	_name = 'vendor.received.document'
	_description = "ETA Vendors Received Documents"
	_inherit = ['mail.thread', 'mail.activity.mixin', 'portal.mixin']
	_check_company_auto = True
	
	state = fields.Selection([('draft', 'Draft'), ('done', 'Done')], "Status", default='draft')
	active = fields.Boolean("Active", default=True)
	company_id = fields.Many2one('res.company', 'Company', default=lambda self: self.env.company.id)
	currency_id = fields.Many2one('res.currency', "Currency",
	                              default=lambda self: self.env.company.currency_id)
	name = fields.Char('Document UUID', required=True)  # uuid
	submission_id = fields.Char('Submission UUID')  # submissionUUID
	document_type = fields.Selection(DOC_TYPES, "Document Type", required=True)  # typeName
	document_version = fields.Selection([('0.9', '0.9'), ('1.0', '1.0')],
	                                    "Document Version", required=True)  # typeVersionName
	document_status = fields.Char("Document Status", required=True)  # typeVersionName
	issuer_id = fields.Char("Issuer ID")  # issuerId
	issuer_name = fields.Char("Issuer Name")  # issuerName
	receiver_id = fields.Char("Receiver ID")  # receiverId
	receiver_name = fields.Char("Receiver Name")  # receiverName
	date_issued = fields.Char("Date Issued")  # dateTimeIssued
	date_received = fields.Char("Date Received")  # dateTimeReceived
	json = fields.Text("Json")
	total_sales = fields.Monetary("Total Sales")
	total_discount = fields.Monetary("Total Discount")
	net_amount = fields.Monetary("Net Amount")
	total = fields.Monetary("Total")
	line_ids = fields.One2many('vendor.received.document.line', 'document_id', "Lines")
	partner_id = fields.Many2one('res.partner', "Vendor", domain=[('supplier_rank', '!=', 0)])
	bill_id = fields.Many2one('account.move', "Bill", domain=[('move_type', '=', 'in_invoice')])
	show_result = fields.Boolean("Show Results?", default=True)
	_sql_constraints = [
		('name_uniq', 'unique (UUID)', "UUID already exists !"),
	]
	
	def action_reset(self):
		for rec in self:
			rec.state = 'draft'
	
	def action_link_bill(self):
		action = self.env.ref('egyptian_electronic_invoice.action_link_bill_wizard').read()[0]
		action['context'] = {
			'document_id': self.id,
			'issuer_id': self.issuer_id,
		}
		return action
	
	def action_reject(self):
		action = self.env.ref('egyptian_electronic_invoice.action_reject_bill_reason_wizard').read()[0]
		action['context'] = {
			'document_id': self.id,
		}
		return action
	
	def action_reject_with_reason(self):
		message = ""
		access_token, client_id, client_secret, apiBaseUrl = self.env.company.sudo().get_access_token()
		headers = {'Content-Type': "application/json", 'cache-control': "no-cache",
		           'Accept': "application/json", "Accept-Language": "ar", 'Authorization': "Bearer %s" % access_token}
		url = apiBaseUrl + '/api/v1.0/documents/%s/state' % self.name
		logging.info(blue + "url: %s" % url + reset)
		try:
			response = requests.request(method='put', url=url, headers=headers, verify=False,
			                            data={"status": "rejected",
			                                  "reason": self.env.context.get('reject_reason') or ""})
			logging.info(yellow + "Response: %s" % response + reset)
		except Exception as e:
			message = "Could Connect to %s due to connection error:\n %s" % (url, e)
			logging.info(red + message + reset)
			raise UserError(message)
		if self.show_result:
			message = "Response Status :%s Reason: %s\n\n" % (response.status_code, response.reason)
		if response.status_code == 404:
			if self.show_result:
				message = "Connecting Egyptian taxes API to submit document respond with error code: [%s]" % response.status_code
				message += "\n\nError Desc.: %s" % response.reason
				raise UserError(_(message))
		elif response.status_code in (200, 202):
			try:
				result = response.json()
			except:
				raise UserError(_("2001: ETA Doesn't respond correctly!!!\n Please try again later"))
			logging.info(green + "Response: %s" % response + reset)
			logging.info(green + "Result: %s" % result + reset)
		# Return Results in view
		if self.show_result:
			res_id = self.env['electronic.invoice.result'].create(
				{'results': message, 'name': "Reject Document Success!!!"})
			action = self.env.ref('egyptian_electronic_invoice.action_electronic_invoice_result').read()[0]
			action['res_id'] = res_id.id
			return action


class VendorReceivedDocumentLine(models.Model):
	_name = 'vendor.received.document.line'
	
	document_id = fields.Many2one('vendor.received.document', "Document")
	name = fields.Char("Product Name", required=True)
	hs_code = fields.Char(string="HS Code", required=True,
	                      help="Standardized code for international shipping and goods declaration."
	                           " At the moment, only used for the FedEx shipping provider.")
	hs_description = fields.Char(string="HS Description", help="Taxpayer System HS Description.")
	hs_type = fields.Selection([('EGS', 'EGS'), ('GS1', 'GS1')], "HS Type", default="GS1",
	                           help="Taxpayer System HS Type.", required=True)
# Ahmed Salama Code End.

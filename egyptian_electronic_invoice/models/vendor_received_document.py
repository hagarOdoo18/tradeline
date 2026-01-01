# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError
import logging
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
	company_id = fields.Many2one('res.company', 'Company', default=lambda self: self.env.user.company_id.id)
	currency_id = fields.Many2one('res.currency', "Currency",
	                              default=lambda self: self.env.user.company_id.currency_id)
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
	partner_id = fields.Many2one('res.partner', "Vendor", domain=[('supplier', '!=', 0)])
	bill_id = fields.Many2one('account.invoice', "Bill", domain=[('type', '=', 'in_invoice')])
	_sql_constraints = [
		('name_uniq', 'unique (name)', "UUID already exists !"),
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

# -*- coding: utf-8 -*-
import base64
import json
import logging
from datetime import datetime

import pytz
import requests
from odoo import models, fields, api, _
from odoo.exceptions import ValidationError, UserError
from datetime import date

grey = "\x1b[38;21m"
yellow = "\x1b[33;21m"
red = "\x1b[31;21m"
bold_red = "\x1b[31;1m"
reset = "\x1b[0m"
green = "\x1b[32m"
blue = "\x1b[34m"
# Ahmed Salama Code Start ---->
PROD_UNIT_TYPE = ['2Z', '4K', '4O', 'A87', 'A93', 'A94', 'AMP', 'ANN', 'B22', 'B49', 'B75', 'B78', 'B84', 'BAR', 'BG',
                  'BO', 'C10', 'C39', 'C41', 'C45', 'C62', 'CA', 'CMK', 'CMQ', 'CMT', 'CS', 'CT', 'CTL', 'D10', 'D33',
                  'D41', 'DAY', 'EA', 'FAR', 'FOT', 'FTK', 'FTQ', 'G42', 'GL', 'GLL', 'GM', 'GRM', 'H63', 'HLT', 'HTZ',
                  'HUR', 'IE', 'INH', 'INK', 'KGM', 'KHZ', 'KMH', 'KMK', 'KMQ', 'KMT', 'KSM', 'KVT', 'KWT', 'LTR', 'M',
                  'MAW', 'MGM', 'MHZ', 'MIN', 'MMK', 'MMQ', 'MMT', 'MON', 'MTK', 'MTQ', 'OHM', 'ONZ', 'PAL', 'PF', 'PK',
                  'SH', 'SMI', 'TNE', 'VLT', 'WEE', 'WTT', 'X03', 'YDQ', 'YRD']
INV_TYP = {'out_invoice': 'I', 'out_refund': 'C'}


class AccountJournalInherit(models.Model):
	_inherit = 'account.journal'
	
	eta_branch = fields.Integer("ETA Branch", default=0)
	eta_branch_code_id = fields.Many2one('res.company.activity', "Branch Activity Code")


class AccountMoveInherit(models.Model):
	_inherit = 'account.move'
	
	# Compute Methods
	@api.onchange('invoice_date', 'e_invoice_sent')
	def _compute_days_until_expire(self):
		"""
		Compute Remain Days until this document can't be send
		"""
		eta_expiration_duration = int(self.env['ir.config_parameter'].sudo().get_param(
			'egyptian_electronic_invoice.eta_expiration_duration'))
		for invoice in self:
			remain_days = 0
			if invoice.invoice_date and not (invoice.valid_flag and invoice.e_invoice_sent):
				remain_days = eta_expiration_duration - (fields.Date.today() - invoice.invoice_date).days
			invoice.expiration_duration = remain_days > 0 and remain_days or 0
	
	@api.onchange('e_invoice_sent', 'expiration_duration')
	def _compute_expired_flag(self):
		for invoice in self:
			expired_flag = False
			if invoice.move_type in ('out_invoice', 'out_refund') and not invoice.expiration_duration \
					and not (invoice.e_invoice_sent and invoice.valid_flag) and invoice.state != 'draft':
				expired_flag = True
			invoice.expired_flag = expired_flag
	
	@api.onchange('e_invoice_uuid')
	def _compute_e_invoice_url(self):
		"""
		Used to generate url for the invoice printed label
		:return:
		"""
		for rec in self:
			e_invoice_url = False
			if self.env.company.config_type == 'uat':
				DocUrl = "https://preprod.invoicing.eta.gov.eg/documents"
			else:
				DocUrl = "https://invoicing.eta.gov.eg/documents"
			if rec.e_invoice_uuid:
				e_invoice_url = "%s/%s" % (DocUrl, rec.e_invoice_uuid)
			rec.e_invoice_url = e_invoice_url
	
	@api.onchange('e_invoice_sent', 'e_invoice_status', 'move_type', 'state', 'e_invoice_uuid')
	def _compute_flags(self):
		for invoice in self:
			valid_flag = submitted_flag = invalid_flag = cancelled_flag = False
			hide_sent_button = hide_cancel_button = True
			if invoice.move_type in ('out_invoice', 'out_refund'):
				if invoice.state == 'posted':
					if invoice.e_invoice_status in ('Draft', 'Cancelled'):
						hide_sent_button = False
					elif invoice.e_invoice_sent and invoice.e_invoice_status == 'Submitted':
						submitted_flag = True
					elif invoice.e_invoice_sent and invoice.e_invoice_status == 'Valid':
						valid_flag = True
						hide_cancel_button = False
					elif invoice.e_invoice_sent and invoice.e_invoice_status == 'Invalid':
						invalid_flag = True
						hide_sent_button = False
				elif invoice.e_invoice_status == 'Cancelled':
					cancelled_flag = True
			invoice.valid_flag = valid_flag
			invoice.submitted_flag = submitted_flag
			invoice.invalid_flag = invalid_flag
			invoice.cancelled_flag = cancelled_flag
			invoice.hide_sent_button = hide_sent_button
			invoice.hide_cancel_button = hide_cancel_button
	
	expired_flag = fields.Boolean(compute=_compute_expired_flag, copy=False)
	submitted_flag = fields.Boolean(compute=_compute_flags, copy=False)
	valid_flag = fields.Boolean(compute=_compute_flags, copy=False)
	invalid_flag = fields.Boolean(compute=_compute_flags, copy=False)
	cancelled_flag = fields.Boolean(compute=_compute_flags, copy=False)
	hide_sent_button = fields.Boolean(compute=_compute_flags, copy=False)
	hide_cancel_button = fields.Boolean(compute=_compute_flags, copy=False)
	
	e_invoice_sent = fields.Boolean("E-Invoice Informed", copy=False,
	                                help="If checked so the Electronic Invoice is sent")
	e_invoice_json = fields.Text("E-Invoice JSON", copy=False)
	e_invoice_canonical = fields.Text("E_invoice Canonical", copy=False)
	
	e_invoice_uuid = fields.Char("E-Invoice ID", readonly=True, copy=False, tracking=True)
	e_invoice_url = fields.Char(compute=_compute_e_invoice_url, string="E-Invoice URL", copy=False, tracking=True)
	e_invoice_date = fields.Datetime("E-Invoice Date", copy=False, tracking=True,
	                                 help="Date of sending E-Invoice, used in validate"
	                                      " 72H in case of cancel invoice")
	e_invoice_cancel_date = fields.Datetime("E-Invoice Cancel Date", copy=False, tracking=True)
	e_invoice_status = fields.Char("E-Invoice Status", help="Status on invoice in Taxes system",
	                               default='Draft', copy=False)
	e_invoice_file = fields.Many2one("ir.attachment", "E-Invoice PDF", copy=False)
	e_invoice_pdf = fields.Binary("PDF", copy=False)
	pdf_name = fields.Char('File Name', copy=False)
	invoice_signed = fields.Boolean("Invoice Signed?", readonly=False, copy=False)
	use_static_signature = fields.Boolean("Use Static Sign?", copy=False)
	static_signature = fields.Text("Static Signature", copy=False)
	static_sign_url = fields.Text("Static Sign URL", copy=False)
	show_results = fields.Boolean('Show Results', copy=False, default=False)
	expiration_duration = fields.Integer("Exp Remain Days", compute=_compute_days_until_expire,
	                                     help="This is show how many days remain until this document expire of sending")
	document_id = fields.Many2one('vendor.received.document', "Document")
	e_invoice_issue = fields.Text("Invalid Reason")
	# TODO: Fields to add manual
	e_invoice_credit_src = fields.Char("Credit Note SRC.")
	e_invoice_so_ref = fields.Char("Sales Order Ref.")
	e_invoice_so_desc = fields.Char("Sales Order Desc.")
	e_invoice_po_ref = fields.Char("Purchase Order Ref.")
	e_invoice_po_desc = fields.Char("Purchase Order Desc.")
	e_invoice_pref_no = fields.Char("Proforma Invoice Num.")
	
	def action_send_electronic_invoice(self):
		logging.info(yellow + "\n---------------------- Start Prepare Send %s Invoice" % len(self) + reset)
		# Generate new token
		access_token, client_id, client_secret, apiBaseUrl = self.env.company.sudo().get_access_token()
		# Assign Headers
		headers = {'Content-Type': "application/json", 'cache-control': "no-cache",
		           'Accept': "application/json", "Accept-Language": "ar",
		           'Authorization': "Bearer %s" % access_token}
		submit_url = apiBaseUrl + "/api/v1/documentsubmissions"
		message = ""
		result_lines = []
		action_name = "Submit Invoice"
		# Loop For Invoice
		invoice_documents = {"documents": []}
		invoice_ids_dict = {}
		show_results = False
		result_json_details = "<b>URL:</b> %s<br/><br/><b>HEADERS:</b> %s <br/><br/><b>CLIENT ID:</b> [%s]<br/><br/>" \
		                      "<b>CLIENT SECRET:</b> [%s]<br/><br/><b>" \
		                      % (submit_url, headers, client_id, client_secret)
		docs_length = len(self)
		if docs_length > self.env.company.max_invoice_per_time:
			raise UserError(_("It's restricted to send maximum %s document per time,\n"
			                  " and you are trying to send %s, Please reduce document to first number."
			                  % (self.env.company.max_invoice_per_time, docs_length)))
		if docs_length > self.env.company.max_invoice_per_batch:
			docs_chunks = [self[x:x + self.env.company.max_invoice_per_batch]
			               for x in range(0, len(self), self.env.company.max_invoice_per_batch)]
		else:
			docs_chunks = [self]
		api_version = self.env.company.config_version
		for chunk in docs_chunks:
			for invoice in chunk:
				show_results = invoice.show_results
				internal_id = invoice.name.replace("/", "")
				# if not invoice.e_invoice_json:
				print("-------------------- here --------------------")
				invoice_params, env = invoice.action_generate_eta_json()
				invoice_params = eval(invoice.e_invoice_json)
				print("invoice_params: ", invoice_params)
				if api_version == '1.0':
					invoice_params = invoice.action_sign_invoice()
				elif api_version == '0.9':
					invoice_params['documentTypeVersion'] = api_version
					invoice_params['signatures'] = [{
						"signatureType": "I",
						"value": ""}]
				else:
					raise UserError(
						_("Company VERSION is not set, you can change it from company-> ETA tab -> VERSION field"))
				invoice_documents['documents'].append(invoice_params)
				invoice_ids_dict[internal_id] = invoice.id
			logging.info(blue + "Request Documents: %s" % invoice_documents + reset)
			details = json.dumps(invoice_documents, indent=4, ensure_ascii=False).encode('utf8')
			try:
				response = requests.post(url=submit_url, headers=headers, data=details, verify=False)
			except Exception as e:
				message = "Could Connect to %s due to connection error:\n %s" % (submit_url, e)
				logging.info(red + message + reset)
				raise ValidationError(message)
			if response.status_code == 404:
				message = "Internal error code:1001" \
				          "\n Connecting Egyptian taxes API to submit document respond with error code: [%s]" % response.status_code
				message += "\n\nError Desc.: %s" % response.reason
				raise ValidationError(_(message))
			elif response.status_code in (200, 202):
				logging.info(green + "Response: %s" % response + reset)
				try:
					result = response.json()
				except:
					raise UserError(_("1001: ETA Doesn't respond correctly!!!\n Please try again later"))
				logging.info(green + "Response content: %s" % response + reset)
				logging.info(green + "Result: %s" % result + reset)
				# Extract Results:
				if result.get('acceptedDocuments'):
					message += "<h4 style='color:green'>Success submit of %s Document <h4><br/>" % len(
						result['acceptedDocuments'])
					for accept_detail in result['acceptedDocuments']:
						submitted_invoice = self.browse(invoice_ids_dict[accept_detail.get('internalId')])
						logging.info(yellow + "submitted_invoice: %s" % submitted_invoice + reset)
						if submitted_invoice:
							# Fill Lines of result wizard
							result_lines.append(
								(0, 0, {'move_id': submitted_invoice.id, 'internalId': accept_detail.get('internalId'),
								        'uuid': accept_detail['uuid'], 'line_action': 'success'}))
							# Check For Status
							document_status = "Submitted"
							logging.info(yellow + "UUID: %s" % accept_detail['uuid'] + reset)
							# TODO:: This option to auto update is stopped because of 404 that is from non saved uuid on taxes yet
							# status_Value = submitted_invoice._action_eta_get_document('raw', accept_detail['uuid'])
							# logging.info(red + "status_Value: %s" % status_Value + reset)
							# if status_Value:
							# 	document_status = status_Value['status']
							# 	submitted_invoice.action_update_electronic_invoice_pdf()
							# Edit Record
							vals = {'e_invoice_uuid': accept_detail['uuid'],
							        'e_invoice_date': fields.Datetime.now(),
							        'e_invoice_sent': True,
							        'e_invoice_status': document_status}
							print("VALS::", vals)
							submitted_invoice.write(vals)
							submitted_invoice.message_post(
								body="<b>E-Invoice Sync to Taxes system  accepted with UUID:</b>"
								     "<span  style='color:green'>%s</span?" % accept_detail['uuid'])
							logging.info(green + "\n Invoice %s Accepted with UUID: </b> %s <br/>" %
							             (accept_detail['internalId'], accept_detail['uuid']) + reset)
				if result.get('rejectedDocuments'):
					message += "<h4 style='color:red'>Errors on submit of %s Document <h4><br/>" % len(
						result['rejectedDocuments'])
					for reject_details in result['rejectedDocuments']:
						submitted_invoice = self.browse(invoice_ids_dict[reject_details.get('internalId')])
						line_error_desc = ""
						e_invoice_issue = ""
						for error in reject_details['error']['details']:
							line_error_desc += "<ul><li><b>Code:</b> %s</li>" % error["code"]
							line_error_desc += "<li><b>Message:</b> %s</li>" % error["message"]
							line_error_desc += "<li><b>Exact field:</b> %s</li></ul>" % error["propertyPath"]
							e_invoice_issue += "%s, " % error["message"]
						if submitted_invoice:
							result_lines.append(
								(0, 0, {'move_id': submitted_invoice.id, 'internalId': reject_details.get('internalId'),
								        'description': line_error_desc, 'line_action': 'error'}))
							submitted_invoice.e_invoice_issue = e_invoice_issue
						logging.info(red + "\n Invoice %s Rejected" % submitted_invoice.display_name + reset)
			else:
				try:
					result = response.json()
				except:
					raise UserError(_("1002: ETA Doesn't respond correctly!!!\n Please try again later"))
				message = "Internal error code:1002" \
				          "\n Connecting Egyptian taxes API to submit document respond with error code: [%s]" % response.status_code
				message += "\n\nError Desc.: %s" % response.reason
				message += "\n\nresult: %s" % result
				raise UserError(_(message))
		if show_results:
			print("SHOWWWWW: ", show_results)
			# Return Results in view
			res_id = self.env['electronic.invoice.result'].create({'results': message, 'name': action_name,
			                                                       'line_ids': result_lines,
			                                                       'json_details': result_json_details})
			action = self.env.ref('egyptian_electronic_invoice.action_electronic_invoice_result').read()[0]
			action['res_id'] = res_id.id
			return action
		return True
	
	def action_cancel_electronic_invoice(self):
		logging.info(yellow + "\n---------------------- Start Cancel %s Invoice" % len(self) + reset)
		for invoice in self:
			uuid = invoice.e_invoice_uuid
			if uuid:
				now = fields.Datetime.now()
				duration = (now - invoice.e_invoice_date)
				if INV_TYP[invoice.move_type] == "I" and duration.days >= 3:
					raise ValidationError(
						_("Egyptian Taxes Authority prevent cancel Invoices after 3 days of submit date: %s"
						  % invoice.e_invoice_date))
				if INV_TYP[invoice.move_type] == "C" and (duration.seconds / 3600) >= 26:
					raise ValidationError(
						_("Egyptian Taxes Authority prevent cancel Credit Notes after 26 Hour of submit date: %s"
						  % invoice.e_invoice_date))
				
				if not self.env.company.config_type:
					raise ValidationError(
						_("You must select Platform ENVIRONMENT in company %s first." % self.env.company.display_name))
				invoice._custom_invoice_cancel()
				# Generate new token
				access_token, client_id, client_secret, apiBaseUrl = self.env.company.sudo().get_access_token()
				# Assign Headers
				headers = {'Content-Type': "application/json", 'cache-control': "no-cache",
				           'Accept': "application/json", "Accept-Language": "ar",
				           'Authorization': "Bearer %s" % access_token}
				cancel_url = apiBaseUrl + "/api/v1.0/documents/state/%s/state" % uuid
				details = json.dumps({
					"status": "cancelled",
					"reason": "according to system action"
				}, indent=4)
				try:
					response = requests.put(url=cancel_url, headers=headers, data=details, verify=False)
				except Exception as e:
					message = "Could Connect to %s due to connection error:\n %s" % (cancel_url, e)
					logging.info(red + message + reset)
					raise ValidationError(message)
				if response.status_code == 404:
					message = "Connecting Egyptian taxes API to cancel document respond with error code: [%s]" % response.status_code
					message += "\n\nError Desc.: %s" % response.reason
					raise ValidationError(_(message))
				elif response.status_code in (200, 202):
					try:
						result = response.json()
					except:
						raise UserError(_("1003: ETA Doesn't respond correctly!!!\n Please try again later"))
					message = "Cancel Document with ref: %s" % invoice.e_invoice_uuid
					invoice.message_post(body="<b style='color:red'>%s</b>" % message)
					invoice.e_invoice_status = 'Cancelled'
					invoice.e_invoice_cancel_date = fields.Datetime.now()
					if invoice.show_results:
						# Return Results in view
						res_id = self.env['electronic.invoice.result'].create(
							{'results': message, 'name': "Document Cancel"})
						action = self.env.ref('egyptian_electronic_invoice.action_electronic_invoice_result').read()[0]
						action['res_id'] = res_id.id
						return action
					logging.info(green + "Response: %s" % response + reset)
					logging.info(green + "Result: %s" % result + reset)
				else:
					try:
						result = response.json()
					except:
						raise UserError(_("1004: ETA Doesn't respond correctly!!!\n Please try again later"))
					message = "Connecting Egyptian taxes API to Cancel document respond with error code: [%s]" % response.status_code
					message += "\n\nError Desc.: %s" % response.reason
					message += "\n\nURL: %s" % cancel_url
					message += "\n\nDetails: %s" % result
					message += "\n\nJSON: %s" % details
					
					raise UserError(_(message))
			else:
				raise ValidationError(_("There is no Document UUID to be canceled"))
	
	def action_update_electronic_invoice_status(self):
		action = True
		for invoice in self:
			result = invoice._action_eta_get_document('raw', invoice.e_invoice_uuid)
			invoice.write({
				'e_invoice_status': result['status'],
				'e_invoice_sent': True,
			})
			action_name = "Get Document %s Details " % result['internalId']
			validationResults = result.get('validationResults')
			message = "<h4 style='color:green'>Check Result is: %s<h4><br/>" % validationResults.get('status')
			result_lines = []
			for accept_detail in validationResults.get('validationSteps'):
				# Fill Lines of result wizard
				description = "<ul><li><b>Code:</b> %s</li>" % accept_detail.get("name")
				description += "<li><b>Status:</b> %s</li>" % accept_detail.get("status")
				description += "<li><b>error:</b> %s</li>" % accept_detail.get("error")
				description += "</ul>"
				if accept_detail.get('status') == 'Invalid':
					invoice.e_invoice_issue = accept_detail.get('error')
				result_lines.append(
					(0, 0, {'internalId': result.get('internalId'), 'description': description,
					        'uuid': result.get('uuid'),
					        'line_action': 'success' if accept_detail.get("status") == 'Valid' else 'error'}))
			if invoice and result_lines and invoice.show_results:
				# Return Results in view
				res_id = self.env['electronic.invoice.result'].create({'results': message, 'name': action_name,
				                                                       'line_ids': result_lines,
				                                                       'json_details': str(result)})
				action = self.env.ref('egyptian_electronic_invoice.action_electronic_invoice_result').read()[0]
				action['res_id'] = res_id.id
		return action
	
	def action_update_electronic_invoice_pdf(self):
		for invoice in self.filtered(lambda l: l.e_invoice_sent):
			result = invoice._action_eta_get_document('pdf', invoice.e_invoice_uuid)
			attachment = invoice._create_eta_attachment(result)
			invoice.e_invoice_file = attachment
			invoice.e_invoice_pdf = attachment.datas
			invoice.pdf_name = attachment.name
	
	# Helper Methods
	def _create_eta_attachment(self, datas):
		content = base64.b64encode(datas)
		filename = ("%s-%s.pdf" % (self.name, self.e_invoice_uuid)).replace('/', '_')
		return self.env['ir.attachment'].sudo().create({
			'name': filename,
			'datas': content,
			'store_fname': filename,
			'mimetype': 'application/x-pdf',
			'type': 'binary',
			'res_model': self._name,
			'res_id': self.id
		})
	
	def _get_eta_personal_details(self, partner, order_total):
		required_fields = ['country_id', 'state_id', 'city', 'street', 'classification']
		for field_name in required_fields:
			if not getattr(partner, field_name):
				raise ValidationError(
					_("Missing one of required details [%s] for partner [%s]" % (field_name, partner.display_name)))
		if not partner.vat:
			if partner.classification == 'B':
				raise ValidationError(_("Missing Tax ID for partner [%s]" % partner.display_name))
			elif partner.classification == 'P' and order_total > 50000:
				raise ValidationError(
					_("Missing National ID for partner [%s] cause order over 50K" % partner.display_name))
		if partner.classification == 'P' and order_total > 50000 and len(partner.vat) != 14:
			raise ValidationError(_("National ID for partner [%s] is not valid" % partner.display_name))
		return {"address": {"country": partner.country_id.code,
		                    "governate": partner.state_id.display_name,
		                    "regionCity": partner.city,
		                    "street": partner.street,
		                    "buildingNumber": partner.street2 if partner.street2 else '1',
		                    "postalCode": partner.zip or "12345",  # TODO:: Next is optional
		                    "floor": partner.floor or "0",
		                    "room": partner.room or "0",
		                    "landmark": partner.landmark or "landmark",
		                    "additionalInformation": partner.additional_info or "additionalInformation"
		                    },
		        "type": partner.classification,
		        "id": partner.vat or "",  # 538486562
		        "name": partner.display_name or ""
		        }
	
	def _get_eta_invoice_lines(self):
		invoice_lines = []
		totalAmount = 0.00000
		total_discount = 0.00000
		total_sales_amount = 0.00000
		EGP = self.env.ref('base.EGP')
		for line in self.invoice_line_ids.filtered(
				lambda l: l.product_id):  # Loop only lines with product to avoid line of T8
			price_unit = line.get_price_unit()
			amountEGP = price_unit if price_unit else 1.00
			currencySold = EGP.name
			amountSold = 0.00
			currencyExchangeRate = 0.00
			# line_price_total = line.price_total
			sum_line_taxes_no_deduction = sum(tax.amount for tax in line.tax_ids if not tax.is_deduction) / 100
			line_price_total = price_unit * line.quantity * (1 - line.discount / 100) * (
					1 + sum_line_taxes_no_deduction)
			if line.currency_id and line.currency_id != EGP:
				# TODO: NEEDED to be changed
				#amountEGP = line.price_unit * currencyExchangeRate
				amountSold = price_unit if price_unit else 1.00
				currencySold = line.currency_id.name
				currencyExchangeRate = round(1 / line.currency_id.rate,5)
				amountEGP = round(line.price_unit * currencyExchangeRate,5)
				# line_price_total = round(line.price_total * currencyExchangeRate,5)
				line_price_total = price_unit * line.quantity * (1 - line.discount / 100) * (
						1 + sum_line_taxes_no_deduction) * currencyExchangeRate
			price_unit_wo_discount = line.price_unit * (1 - (line.discount / 100.0))
			discount_percentage = line.discount if line.discount else 0.00000
			quantity = line.quantity
			sales_total_amount = amountEGP * quantity
			discount_amount = (discount_percentage / 100) * sales_total_amount
			total_discount += discount_amount
			taxes_res = line.tax_ids._origin.compute_all(price_unit_wo_discount,
			                                             quantity=line.quantity, currency=line.currency_id,
			                                             product=line.product_id,
			                                             partner=line.partner_id)
			total_sales_amount += sales_total_amount
			prd_required_fields = ['e_invoicing_code', 'code_type', 'ar_description']
			for prd_field in prd_required_fields:
				if not getattr(line.product_id, prd_field):
					raise ValidationError(_("Missing One of required details [%s] for product [%s] !!!" %
					                        (prd_field, line.product_id.display_name)))
			uom = line.product_uom_id.eta_uom_id and line.product_uom_id.eta_uom_id.code or line.product_uom_id.name
			if uom not in PROD_UNIT_TYPE:
				raise ValidationError(_("This product uit of measure [%s] not in Tax System unites \n\n %s" %
				                        (uom, PROD_UNIT_TYPE)))
			unitType = uom or "D41"
			netTotal = sales_total_amount - discount_amount
			taxable_items_lines, totalTaxableFees = line._get_taxableItems(taxes_res['taxes'])
			totalAmount += line_price_total
			eta_description = self.env['ir.config_parameter'].sudo().get_param('egyptian_electronic_invoice.eta_description')
			description = eta_description == 'product' and line.product_id.display_name or line.name
			if quantity != 0.0:
				invoice_lines.append({
					"description": description or '',  # "Computerl"
					"itemType": line.product_id.code_type,  # "EGS"/"GS1"
					"itemCode": line.product_id.e_invoicing_code,  # "EG-113317713-123456"
					"unitType": unitType,
					"quantity": quantity,
					"internalCode": line.product_id.barcode or "",  # "ICO"/default_code
					"salesTotal": round(sales_total_amount, 5),  # Total Quantity
					"total": round(line_price_total, 5),
					"valueDifference": 0.00,  # TODO::  لازم تبقى 0 دايما (خاصه بالعينات المجانيه)
					"totalTaxableFees": totalTaxableFees,  # TODO::  In CASE of [T5:T12] must sent
					"netTotal": round(netTotal, 5),
					"itemsDiscount": 0.00,  # TODO::  THIS VALUE NOT USED IN ODOO خصم اضافى على المنتجات كقيمه
					"unitValue": {
						"currencySold": currencySold,  # Currency Code
						"amountSold": round(amountSold, 5),  # amount value in currency
						"currencyExchangeRate": round(currencyExchangeRate, 5),  # currency Rate
						"amountEGP": round(amountEGP, 5)  # Amount in EGP
					},
					"discount": {  # TODO::  الخصم قبل حساب الضريبه
						"rate": round(discount_percentage, 5),
						"amount": round(discount_amount, 5)},
					"taxableItems": taxable_items_lines,
				})
		return invoice_lines, total_discount, total_sales_amount, totalAmount
	
	def _get_eta_tax_totals(self, tax_lines):
		taxTotals = []
		if tax_lines:
			for line in tax_lines:
				taxType = line.tax_line_id.type_code_id.code or "T1"
				taxTotals.append({"taxType": taxType,
				                  "amount": round(abs(line.balance), 5)})
		else:
			taxType = self.env.company.type_code_id.code or "T1"
			taxTotals.append({"taxType": taxType,
			                  "amount": 0.00})
		return taxTotals
	
	def get_payment_data(self):
		return {
			"bankName": "",
			"bankAddress": "",
			"bankAccountNo": "",
			"bankAccountIBAN": "",
			"swiftCode": "",
			"terms": ""
		}
	
	def get_delivery_data(self):
		return {
			"approach": "",
			"packaging": "",
			"dateValidity": "",
			"exportPort": "",
			"countryOfOrigin": "EG",
			"grossWeight": 0,
			"netWeight": 0,
			"terms": ""
		}
	
	def _action_eta_get_document(self, endpoint, e_invoice_uuid):
		access_token, client_id, client_secret, apiBaseUrl = self.env.company.sudo().get_access_token()
		headers = {'Content-Type': "application/json", 'cache-control': "no-cache",
		           'Accept': "application/json",
		           'Authorization': "Bearer %s" % access_token}
		logging.info(yellow + "UUID: %s" % e_invoice_uuid + reset)
		get_details_url = apiBaseUrl + "/api/v1/documents/%s/%s" % (e_invoice_uuid, endpoint)
		logging.info(yellow + "get_details_url: %s" % get_details_url + reset)
		try:
			response = requests.get(url=get_details_url, headers=headers, verify=False)
		except Exception as e:
			message = "Could Connect to %s due to connection error:\n %s" % (get_details_url, e)
			logging.info(red + message + reset)
			raise ValidationError(message)
		logging.info(green + "response: %s" % response + reset)
		if response.status_code == 404:
			message = "Connecting Egyptian taxes API to get document respond with error code: [%s]" % response.status_code
			message += "\n\nError Desc.: %s" % response.reason
			message += "\n\nURL: %s" % get_details_url
			raise ValidationError(_(message))
		elif response.status_code in (200, 202):
			result = response
			if endpoint == 'raw':
				try:
					result = response.json()
				except:
					raise UserError(_("1004: ETA Doesn't respond correctly!!!\n Please try again later"))
				logging.info(green + "Result: %s" % result + reset)
			elif endpoint == 'pdf':
				try:
					result = response.content
				except:
					raise UserError(_("1005: ETA Doesn't respond correctly!!!\n Please try again later"))
			return result
		else:
			try:
				result = response.json()
			except:
				raise UserError(_("1006: ETA Doesn't respond correctly!!!\n Please try again later"))
			message = "Connecting Egyptian taxes API to Cancel document respond with error code: [%s]" % response.status_code
			message += "\n\nError Desc.: %s" % response.reason
			message += "\n\nURL: %s" % get_details_url
			message += "\n\nDetails: %s" % result
			raise UserError(_(message))
	
	def _get_params_canonical(self, invoice_params):
		serialized = ''
		for key, value in invoice_params.items():
			serialized += '"%s"' % key
			if isinstance(value, dict):
				for d_key, d_value in value.items():
					serialized += '"%s"' % d_key
					if isinstance(d_value, dict):
						for dd_key, dd_value in d_value.items():
							serialized += '"%s""%s"' % (dd_key, dd_value)
					else:
						serialized += '"%s""%s"' % (d_key, d_value)
			elif isinstance(value, list):
				for line in value:
					if isinstance(line, dict):
						for l_key, l_value in line.items():
							serialized += '"%s"' % l_key
							if isinstance(l_value, dict):
								for d_key, d_value in l_value.items():
									serialized += '"%s""%s"' % (d_key, d_value)
							if isinstance(l_value, list):
								for ll_value in l_value:
									for lld_key, lld_value in ll_value.items():
										serialized += '"%s""%s"' % (lld_key, lld_value)
							else:
								serialized += '"%s"' % l_value
			
			else:
				serialized += '"%s""%s"' % (key, value)
		serialized = serialized.replace('\n', '')
		serialized = serialized.replace('\t', '')
		return serialized
	
	def get_signature_value(self):
		logging.info(green + "============================Start Signing Docs===============" + reset)
		for invoice in self:
			signature = False
			if self.env.company.signature_tool == 'python':
				if not invoice.e_invoice_canonical:
					# Regenerate Json
					invoice_params, env = invoice.action_generate_eta_json()
				static_sign_url = "%s" % self.env.company.signature_url
				user_pin = self.env.company.signature_pin or '52510779'
				token_label = self.env.company.signature_label or '52510779'
				invoice.static_sign_url = static_sign_url
				logging.info(green + "static_sign_url: %s" % static_sign_url + reset)
				try:
					# invoice_params = json.dumps(invoice.e_invoice_canonical, indent=4, ensure_ascii=False).encode('utf8')
					invoice_params = invoice.e_invoice_canonical
					logging.info(green + "DATA: %s" % {'data': invoice_params,
					                                   'token_label': token_label, 'user_pin': user_pin} + reset)
					response = requests.get(url=static_sign_url,
					                        data={'data': invoice_params,
					                              'token_label': token_label, 'user_pin': user_pin},
					                        verify=False)
					status_code = response.status_code
					print("status_code: ", status_code)
					response = response.text
					if status_code != 200:
						raise UserError("Error %s\nDetails:  %s" % (status_code, response))
					else:
						signature = response
				except Exception as e:
					message = "Could Connect to %s due to connection error:\n %s" % (static_sign_url, e)
					logging.info(red + message + reset)
					raise UserError(message)
			
			# TODO:: no more depend on it
			elif self.env.company.signature_tool == 'c#':
				req_body = eval(invoice.e_invoice_json)
				invoice_params = json.dumps(req_body, indent=4, ensure_ascii=False).encode('utf8')
				try:
					serialized = requests.post(url=self.env.company.signature_serializer,
					                           data=invoice_params, verify=False).content
				except Exception as e:
					message = "Could Connect to %s due to connection error:\n %s" % (
						self.env.company.signature_serializer, e)
					logging.info(red + message + reset)
					raise ValidationError(message)
				logging.info(green + "serialized: %s" % serialized + reset)
				try:
					hashed = requests.post(url=self.env.company.signature_hash, data=serialized, verify=False).content
				except Exception as e:
					message = "Could Connect to %s due to connection error:\n %s" % (self.env.company.signature_hash, e)
					logging.info(red + message + reset)
					raise ValidationError(message)
				logging.info(green + "hashed: %s" % hashed + reset)
				invoice.e_invoice_canonical = serialized
				signature = hashed
			self._cr.commit()
			invoice.static_signature = signature
			return signature
	
	def action_generate_eta_json(self):
		env = self.env.company.config_type
		if not env:
			raise ValidationError(
				_("You must select Platform ENVIRONMENT in company %s first." % self.env.company.display_name))
		if not self.invoice_date:
			raise ValidationError(_("You must Add invoice date first!!\n technical name 'invoice_date'"))
		api_version = self.env.company.config_version

		invoice_time = datetime.combine(date.today(), datetime.min.time())
		invoice_lines, totalDiscountAmount, totalSalesAmount, totalAmount = self._get_eta_invoice_lines()
		netAmount = (totalSalesAmount - totalDiscountAmount)
		tax_lines = self.line_ids.filtered(lambda l: l.tax_line_id and l.tax_line_id.type_code_id)
		invoice_time = invoice_time.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
		taxpayerActivityCode = self.journal_id.eta_branch_code_id and self.journal_id.eta_branch_code_id.code or \
		                       self.env.company.activity_code_id.code or "1061"
		issuer = self._get_eta_personal_details(self.env.company.partner_id, totalSalesAmount)
		issuer["address"]["branchID"] = "%s" % self.journal_id.eta_branch or 0
		receiver = self._get_eta_personal_details(self.partner_id, totalSalesAmount)
		internal_id = self.name.replace("/", "")
		tax_total_lines = self._get_eta_tax_totals(tax_lines)
		invoice_params = {
			"issuer": issuer,
			"receiver": receiver,
			"documentType": INV_TYP[self.move_type],  # "I/C/D"
			"documentTypeVersion": api_version,
			"dateTimeIssued": invoice_time,
			"taxpayerActivityCode": taxpayerActivityCode,
			"internalID": internal_id,  # "IID1"
			"payment": self.get_payment_data(),
			"delivery": self.get_delivery_data(),
			"purchaseOrderReference": self.e_invoice_po_ref or "",
			"purchaseOrderDescription": self.e_invoice_po_desc or "",
			"salesOrderReference": self.e_invoice_so_ref or self.ref or "",
			"salesOrderDescription": self.e_invoice_so_desc or "Normal VAT Sale Orders",
			"proformaInvoiceNumber": self.e_invoice_pref_no or "",
			# TODO: ADD Payment
			"invoiceLines": invoice_lines,
			"totalDiscountAmount": round(totalDiscountAmount, 5),
			"totalSalesAmount": round(totalSalesAmount, 5),
			"netAmount": round(netAmount, 5),
			"taxTotals": tax_total_lines,
			"totalAmount": round(totalAmount, 5),
			"extraDiscountAmount": 0.00,  # TODO::  THIS VALUE NOT USED IN ODOO
			# الخصم الإضافى بعد حساب الضريبه(لاتؤثر على الضريبه نهائى)
			"totalItemsDiscountAmount": 0.00,  # TODO::  لازم تبقى 0 دايما ومش عارفين السبب
			
		}
		
		# Check For Sales Ref's
		sale_installed = self.env['ir.module.module'].search([('name', '=', 'sale')])
		if not sale_installed or sale_installed.state == 'installed':
			invoice_params = self._check_sales_ref(invoice_params)
		invoice_params = self._check_extra_fields(invoice_params)
		self.e_invoice_json = str(invoice_params).encode('utf-8')
		self.e_invoice_canonical = self.generate_canonical_manual(
			invoice_params)  # TODO: to be replaced in case of full package
		# self.e_invoice_canonical = self.generate_canonical()  # TODO: to be replaced in case of full package
		self.invoice_signed = False
		return invoice_params, env
	
	def _check_extra_fields(self, invoice_params):
		"""
		This method will be implemented next
		:param invoice_params:
		:return: invoice_params
		"""
		return invoice_params
	
	def action_sign_invoice(self):
		if not self.e_invoice_json or not self.e_invoice_canonical:
			invoice_params, env = self.action_generate_eta_json()
		else:
			invoice_params = eval(self.e_invoice_json)
		signature = self.get_signature_value()
		invoice_params['signatures'] = [{
			"signatureType": "I",
			"value": signature}]
		self.e_invoice_json = invoice_params
		self.invoice_signed = True
		return invoice_params
	
	def _custom_invoice_cancel(self):
		"""
		Do full odoo cycle of draft and cancel
		:return:
		"""
		self.button_draft()
		self.button_cancel()
	
	# Bulk Actions
	def multi_invoice_sync(self):
		"""
		multi sync invoices from tree view
		"""
		active_ids = self.env.context.get('active_ids')
		posted_invoices_ids = self.browse(active_ids).filtered(
			lambda i: not i.valid_flag and i.expiration_duration > 0 and i.state == 'posted' and i.move_type in ('out_invoice', 'out_refund'))
		if not posted_invoices_ids:
			raise ValidationError(_("No Valid records to be used in Sync details!!!"))
		posted_invoices_ids.action_send_electronic_invoice()
	
	def multi_update_status(self):
		"""
		multi sync invoices from tree view
		"""
		sent_invoices_ids = self.browse(self.env.context.get('active_ids')).filtered(
			lambda i: not i.valid_flag and i.state == 'posted' and i.move_type in (
				'out_invoice', 'out_refund') and i.e_invoice_sent)
		if not sent_invoices_ids:
			raise ValidationError(_("No Valid records to be used in update sent documents details!!!"))
		sent_invoices_ids.action_update_electronic_invoice_status()
	
	def multi_invoice_cancel(self):
		"""
		Bulk cancel multi records
		"""
		canceled_invoices_ids = self.browse(self.env.context.get('active_ids')).filtered(
			lambda i: i.e_invoice_uuid and i.state == 'posted' and
			          i.move_type in ('out_invoice', 'out_refund') and i.e_invoice_status == 'Valid')
		if not canceled_invoices_ids:
			raise ValidationError(_("No Valid records to be used in cancel documents!!!"))
		canceled_invoices_ids.action_cancel_electronic_invoice()
	
	# Automatic Actions
	def post(self):
		"""
		If setting Option for auto sync direct with post action
		:return:
		"""
		res = super(AccountMoveInherit, self).post()
		self._check_old_e_invoice_data()
		auto_validate_documents = self.env['ir.config_parameter'].sudo().get_param(
			'egyptian_electronic_invoice.auto_validate_documents')
		if auto_validate_documents:
			ready_documents = self.filtered(lambda l: l.move_type in ('out_invoice', 'out_refund') and
			                                          l.state == 'posted' and not l.valid_flag)
			if ready_documents:
				ready_documents.action_send_electronic_invoice()
		return res
	
	def button_draft(self):
		self._reset_e_invoice_Fields()
		return super(AccountMoveInherit, self).button_draft()
	
	def _check_sales_ref(self, invoice_params):
		"""
		Check if there is sales ref
		:param invoice_params: invoice params after append
		"""
		related_sales = self.invoice_line_ids.filtered(lambda l: l.sale_line_ids)
		sales_list = []
		if related_sales:
			for line in related_sales:
				for sol in line.sale_line_ids:
					if sol.order_id.name not in sales_list:
						sales_list.append(sol.order_id.name)
		if sales_list:
			sale_refs = '/'.join(sales_list)
			invoice_params['salesOrderReference'] = sale_refs[:20]
		return invoice_params
	
	def _check_old_e_invoice_data(self):
		"""
		Reset Old data fields
		"""
		for invoice in self:
			if invoice.e_invoice_sent:
				invoice.e_invoice_sent = False
				invoice.e_invoice_date = False
				invoice.e_invoice_status = False
				invoice.e_invoice_uuid = False
				invoice.e_invoice_url = False
				invoice.invoice_signed = False
				invoice.static_signature = False
	
	def _reset_e_invoice_Fields(self):
		for invoice in self:
			invoice.e_invoice_sent = False
			invoice.e_invoice_date = False
			invoice.e_invoice_status = 'Draft'
			invoice.e_invoice_uuid = False
			invoice.e_invoice_url = False
			invoice.e_invoice_json = False
			invoice.e_invoice_canonical = False
			invoice.invoice_signed = False
			invoice.static_signature = False
	
	def generate_canonical(self):
		url = "http://0.0.0.0:6336/serialize"
		response = requests.get(url=url,
		                        data={'data': self.e_invoice_json}, verify=False).content
		response = eval(response)
		print("RES:: ", response)
		if response.get('status') == 200:
			signature = response.get('value')
		else:
			raise UserError("Error %s\nDetails:  %s" % (response.get('status'), response.get('value')))
		print("signature: ", signature)
		return signature
	
	# TODO LOCAL CANONICAL PART:
	def generate_canonical_manual(self, invoice_params):
		# serialized = self.check_type(invoice_params)
		# serialized = serialized.replace('\n', '')
		# serialized = serialized.replace('\t', '')
		serialized = fromObjecttoUpperCaseString(invoice_params)
		logging.info(green + "=====>  serialized = %s " % serialized + reset)
		return serialized
	
	def check_type(self, value, serialized=''):
		if isinstance(value, dict):
			serialized = self.handle_dict(value, serialized)
		elif isinstance(value, list):
			serialized = self.handle_list(value, serialized)
		else:
			serialized += '"%s"' % value
		return serialized
	
	def handle_dict(self, value, serialized):
		for d_key, d_value in value.items():
			serialized += '"%s"' % d_key.upper()
			serialized = self.check_type(d_value, serialized)
		return serialized
	
	def handle_list(self, value, serialized):
		for l_value in value:
			serialized = self.check_type(l_value, serialized)
		return serialized


class AccountMoveLineInherit(models.Model):
	_inherit = 'account.move.line'
	
	def _get_taxableItems(self, taxes_res):
		"""
		Compute taxable lines
		:param taxes_res:
		:return: taxableItems
		"""
		tax_obj = self.env['account.tax']
		taxableItems = []
		EGP = self.env.ref('base.EGP')
		totalTaxableFees = 0.0
		if taxes_res:
			for tax_line in taxes_res:
				tax = tax_obj.browse(tax_line['id'])
				amount = abs(tax_line['amount'])
				if tax.type_code_id.code in ['T5', 'T6', 'T7', 'T8', 'T9', 'T10', 'T11', 'T12']:
					totalTaxableFees += amount
				if self.currency_id:
					# TODO: NEEDED to be changed
					amount = self.currency_id._convert(
								amount,
								EGP,
								self.company_id,
								self.move_id.invoice_date or fields.Date.today()
							)

					

				taxType = tax.type_code_id.code or self.env.company.type_code_id.code
				subType = tax.sub_type_code_id.code or self.env.company.sub_type_code_id.code
				rate = abs(tax.amount)
				if not tax.is_deduction:
					taxableItems.append({
						"taxType": taxType,
						"amount": round(amount, 5),
						"subType": subType,
						"rate": round(rate, 5)
					})
		else:
			taxableItems = [{"taxType": self.env.company.type_code_id.code,
			                 "amount": 0.00,
			                 "subType": self.env.company.sub_type_code_id.code,
			                 "rate": 0.00}]
		return taxableItems, totalTaxableFees
	
	def get_price_unit(self):
		price_unit_wo_discount = self.price_unit * (1 - (self.discount / 100.0))
		price_unit = self.price_unit
		if self.tax_ids:
			taxes_included = self.tax_ids.filtered(lambda tx: tx.price_include)
			for tax in taxes_included:
				tax_line = tax._origin.compute_all(price_unit_wo_discount,
				                                   quantity=1, currency=self.currency_id,
				                                   product=self.product_id,
				                                   partner=self.partner_id)
				print("Tax_ Line",  tax_line)
				price_unit -= tax_line['taxes'][0]['amount']
		return price_unit


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
# Ahmed Salama Code End.

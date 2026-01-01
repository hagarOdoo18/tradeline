# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import ValidationError
# Ahmed Salama Code Start ---->


class LinkBillWizard(models.TransientModel):
	_name = 'link.bill.wizard'
	_description = "Link Bill Action"
	
	@api.model
	def default_get(self, fields):
		defaults = super(LinkBillWizard, self).default_get(fields)
		if self.env.context.get('document_id'):
			defaults['document_id'] = self.env['vendor.received.document'].browse(self.env.context.get('document_id'))
		if self.env.context.get('issuer_id'):
			defaults['partner_id'] = self.env['res.partner'].search([('vat', '=', self.env.context.get('issuer_id'))])
		return defaults
	
	partner_id = fields.Many2one('res.partner', "Vendor", domain=[('supplier', '!=', 0)], required=True)
	bill_id = fields.Many2one('account.invoice', "Bill", required=True)
	document_id = fields.Many2one('vendor.received.document', "Document")
	
	def action_link(self):
		if self.partner_id and self.bill_id:
			self.bill_id.write({'document_id': self.document_id})
			self.document_id.write({
				'partner_id': self.partner_id,
				'bill_id': self.bill_id,
				'state': 'done',
			})
		else:
			raise UserWarning(_("Something went wrong, Please insure selected values."))
		
# Ahmed Salama Code End.

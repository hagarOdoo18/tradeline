# -*- coding: utf-8 -*-
from odoo import models, fields
import re
import logging

grey = "\x1b[38;21m"
yellow = "\x1b[33;21m"
red = "\x1b[31;21m"
bold_red = "\x1b[31;1m"
reset = "\x1b[0m"
green = "\x1b[32m"
blue = "\x1b[34m"
CLASSIFICATIONS = [('P', 'Personal'), ('B', 'Business'), ('F', 'Foreigner')]


# Ahmed Salama Code Start ---->


class ResPartnerInherit(models.Model):
	_inherit = 'res.partner'
	
	classification = fields.Selection(CLASSIFICATIONS, "Classification", default='P',
	                                  help="Type of customer {'P':'Personal', 'B':'Business', 'F':'Foreigner'}")
	# Address Details
	floor = fields.Char("Floor")
	room = fields.Char("Room")
	landmark = fields.Char("Landmark")
	additional_info = fields.Char("Additional Info.")
	
	def write(self, vals):
		logging.info(blue + "======== Check Vat Field ====" + reset)
		auto_choose_class = self.env['ir.config_parameter'].sudo().get_param(
			'egyptian_electronic_invoice.auto_choose_class')
		if vals.get('vat') and auto_choose_class:
			nat_id_regex = "^([1-9]{1})([0-9]{2})([0-9]{2})([0-9]{2})([0-9]{2})[0-9]{3}([0-9]{1})[0-9]{1}$"
			vat_id_regex = "^[0-9]\d{8}$"
			for rec in self:
				classification = vals.get('classification') or rec.classification
				if re.match(nat_id_regex, vals.get('vat')):
					logging.info(yellow + " ---- Match National ID----" + reset)
					vals['classification'] = 'P'
				elif re.match(vat_id_regex, vals.get('vat')):
					logging.info(yellow + " ---- Match VAT Num.----" + reset)
					vals['classification'] = 'B'
				elif classification != 'F':
					logging.info(yellow + " ---- No vat matching.----" + reset)
					vals['classification'] = 'F'
		return super(ResPartnerInherit, self).write(vals)
# Ahmed Salama Code End.

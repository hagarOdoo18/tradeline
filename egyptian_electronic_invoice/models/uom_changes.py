# -*- coding: utf-8 -*-
import json
import os
import logging

from odoo import models, fields, api
from odoo.osv import expression

igrey = '\x1b[38;21m'
yellow = '\x1b[33;21m'
red = '\x1b[31;21m'
bold_red = '\x1b[31;1m'
reset = '\x1b[0m'
green = '\x1b[32m'
blue = '\x1b[34m'
# Ahmed Salama Code Start ---->


class UomInherit(models.Model):
	_inherit = 'uom.uom'
	
	eta_uom_id = fields.Many2one('eta.uom', 'ETA UOM')


class AccountTaxSubType(models.Model):
	_name = 'eta.uom'
	_description = "ETA UOM"
	
	code = fields.Char('Code')
	name = fields.Char('Name', required=True)
	
	@api.depends('name', 'note')
	def name_get(self):
		"""
		Display name related to active user lang
		"""
		result = []
		for uom in self:
			name = uom.code
			if uom.name:
				name = "%s(%s)" % (uom.code, uom.name)
			result.append((uom.id, name))
		return result
	
	@api.model
	def _name_search(self, name, args=None, operator='ilike', limit=100, name_get_uid=None):
		"""
		Add Search with Name/Ar Name/ Code
		:param name:
		:param args:
		:param operator:
		:param limit:
		:param name_get_uid:
		:return:
		"""
		args = args or []
		domain = []
		if name:
			domain = ['|', ('name', operator, name), ('code', operator, name)]
		return self._search(expression.AND([domain, args]), limit=limit, access_rights_uid=name_get_uid)
	
	@api.model
	def load_eta_uom(self):
		"""
		Load All uom from JSON file
		:return:
		"""
		print("---- 11 ----")
		uom_obj = self.env['eta.uom']
		print("---- in ----")
		with open(os.path.join(os.path.dirname(__file__), '../data/UnitTypes.json'), 'r', encoding='utf8') as EtaUom:
			print("EtaUom: ", EtaUom)
			details = eval(json.dumps(json.load(EtaUom), indent=4))
		if details:
			print("Details: ", details)
			current_uom = uom_obj.search([]).mapped('code')
			for eta_uom in details:
				if eta_uom.get('code') and eta_uom.get('code') not in current_uom:
					uom_obj.create({
						'code': eta_uom.get('code'),
						'name': eta_uom.get('desc_en')
						
					})
					logging.info(green + "Create New UOM: %s" % eta_uom.get('Code') + reset)
# Ahmed Salama Code End.

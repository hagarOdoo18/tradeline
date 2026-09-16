# -*- coding: utf-8 -*-
import json
import logging
import os

from odoo import models, fields, api
from odoo.osv import expression

grey = "\x1b[38;21m"
yellow = "\x1b[33;21m"
red = "\x1b[31;21m"
bold_red = "\x1b[31;1m"
reset = "\x1b[0m"
green = "\x1b[32m"
blue = "\x1b[34m"

# Ahmed Salama Code Start ---->


class AccountTaxType(models.Model):
	_name = 'account.tax.type.code'
	_description = "Account Tax Type"
	
	name = fields.Char('Name')
	ar_name = fields.Char('Arabic Name', required=True)
	code = fields.Char("Code", required=True)
	taxable = fields.Boolean("Taxable")
	# Constrain in Code
	_sql_constraints = [
		('unique_tax_type_code', 'unique(code)', 'The Tax Type Code(Code) must be unique!'),
	]
	
	@api.depends('name', 'ar_name')
	def name_get(self):
		"""
		Display name related to active user lang
		"""
		result = []
		for tax in self:
			name = tax.name
			if 'ar' in self.env.user.lang:
				name = tax.ar_name
			result.append((tax.id, name))
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
			domain = ['|', '|', ('name', operator, name), ('ar_name', operator, name), ('code', operator, name)]
		return self._search(expression.AND([domain, args]), limit=limit, access_rights_uid=name_get_uid)
	
	@api.model
	def load_tax_types(self):
		"""
		Load All tax types from JSON file
		:return:
		"""
		tax_type_obj = self.env['account.tax.type.code']
		with open(os.path.join(os.path.dirname(__file__), '../data/TaxTypes.json'), 'r', encoding='utf8') as UnitTypes:
			details = eval(json.dumps(json.load(UnitTypes), indent=4))
		if details:
			current_tax_types = tax_type_obj.search([]).mapped('code')
			for tax_type in details:
				if tax_type.get('Code') and tax_type.get('Code') not in current_tax_types:
					tax_type_obj.create({
						'name': tax_type.get('Desc_en'),
						'ar_name': tax_type.get('Desc_ar'),
						'code': tax_type.get('Code'),
						'taxable': True,
						
					})
					logging.info(green + "Create New Tax: %s" % tax_type.get('Code') + reset)
		with open(os.path.join(os.path.dirname(__file__), '../data/NonTaxableTaxTypes.json'),
		          'r', encoding='utf8') as NonTaxableTaxTypes:
			non_taxable_details = eval(json.dumps(json.load(NonTaxableTaxTypes), indent=4))
		if non_taxable_details:
			current_tax_types = tax_type_obj.search([]).mapped('code')
			for tax_type in non_taxable_details:
				if tax_type.get('Code') and tax_type.get('Code') not in current_tax_types:
					tax_type_obj.create({
						'name': tax_type.get('Desc_en'),
						'ar_name': tax_type.get('Desc_ar'),
						'code': tax_type.get('Code'),
					})
					logging.info(green + "Create New un taxable Tax: %s" % tax_type.get('Code') + reset)


class AccountTaxSubType(models.Model):
	_name = 'account.tax.sub.type.code'
	_description = "Account Tax Sub Type"
	
	name = fields.Char('Name')
	ar_name = fields.Char('Arabic Name', required=True)
	type_id = fields.Many2one('account.tax.type.code', "Type", ondelete='cascade')
	code = fields.Char("Code", required=True)
	# Constrain in Code
	_sql_constraints = [
		('unique_tax_sub_type_code', 'unique(code)', 'The Tax Sub Type Code(Code) must be unique!'),
	]
	
	@api.depends('name', 'ar_name')
	def name_get(self):
		"""
		Display name related to active user lang
		"""
		result = []
		for sub_tax in self:
			name = sub_tax.name
			if 'ar' in self.env.user.lang:
				name = sub_tax.ar_name
			result.append((sub_tax.id, name))
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
			domain = ['|', '|', ('name', operator, name), ('ar_name', operator, name), ('code', operator, name)]
		return self._search(expression.AND([domain, args]), limit=limit, access_rights_uid=name_get_uid)
	
	@api.model
	def load_tax_sub_types(self):
		"""
		Load All tax sub types from JSON file
		:return:
		"""
		tax_type_obj = self.env['account.tax.type.code']
		tax_sub_type_obj = self.env['account.tax.sub.type.code']
		with open(os.path.join(os.path.dirname(__file__), '../data/TaxSubtypes.json'), 'r', encoding='utf8') as TaxSubtypes:
			details = eval(json.dumps(json.load(TaxSubtypes), indent=4))
		if details:
			current_sub_tax_types = tax_sub_type_obj.search([]).mapped('code')
			for sub_tax_type in details:
				type_id = tax_type_obj.search([('code', '=', sub_tax_type.get('TaxtypeReference'))])
				if sub_tax_type.get('Code') and sub_tax_type.get('Code') not in current_sub_tax_types and type_id:
					tax_sub_type_obj.create({
						'name': sub_tax_type.get('Desc_en'),
						'ar_name': sub_tax_type.get('Desc_ar'),
						'code': sub_tax_type.get('Code'),
						'type_id': type_id.id,
						
					})
					logging.info(green + "Create New Sub Tax: %s" % sub_tax_type.get('Code') + reset)


class AccountTaxInherit(models.Model):
	_inherit = 'account.tax'
	
	type_code_id = fields.Many2one('account.tax.type.code', "Tax Type Code",
	                               help="To be used in informing of Egyptian Taxes Foundation.")
	sub_type_code_id = fields.Many2one('account.tax.sub.type.code', "Sub Tax Type Code",
	                                   domain="[('type_id','=',type_code_id)]", ondelete='cascade',
	                                   help="To be used in informing of Egyptian Taxes Foundation.")
	is_deduction = fields.Boolean(string="Is Deduction")
# Ahmed Salama Code End.

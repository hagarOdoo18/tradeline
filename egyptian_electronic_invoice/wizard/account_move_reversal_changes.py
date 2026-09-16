# -*- coding: utf-8 -*-
from odoo import models

igrey = '\x1b[38;21m'
yellow = '\x1b[33;21m'
red = '\x1b[31;21m'
bold_red = '\x1b[31;1m'
reset = '\x1b[0m'
green = '\x1b[32m'
blue = '\x1b[34m'
# Ahmed Salama Code Start ---->


class AccountMoveReversalInherit(models.TransientModel):
	_inherit = 'account.move.reversal'
	
	def _prepare_default_reversal(self, move):
		result = super(AccountMoveReversalInherit, self)._prepare_default_reversal(move)
		if move.e_invoice_uuid:
			result['e_invoice_credit_src'] = move.e_invoice_uuid
		return result

# Ahmed Salama Code End.

"""Accounting invoice lookup independent of the currently open ETA view."""
import re
from urllib.parse import urlsplit

from odoo import api, models, _
from odoo.exceptions import AccessError, UserError
from odoo.osv import expression


class EtaInvoiceWorkspace(models.Model):
    _inherit = 'account.move'

    @api.model
    def action_eta_find_invoice(self, query):
        if not (self.env.su or self.env.user.has_group('account.group_account_readonly')
                or self.env.user.has_group('account.group_account_invoice')):
            raise AccessError(_('Accounting access is required to find invoices.'))
        self.check_access('read')
        if not isinstance(query, str) or not query.strip() or len(query) > 300:
            raise UserError(_('Enter an invoice number, branch reference, or invoice link.'))
        query = query.strip()
        path = urlsplit(query).path if query.startswith(('https://', 'http://', '/odoo/')) else ''
        link = re.search(r'/account\.move/(\d+)(?:/|$)', path)
        if link:
            lookup = [('id', '=', int(link.group(1)))]
        else:
            terms = [query]
            label = re.fullmatch(r'(.+?)\s+\((.+)\)', query)
            if label:
                terms.extend([label.group(1).strip(), label.group(2).strip()])
            lookup = expression.OR([[(column, 'ilike', term)] for term in terms
                for column in ('name', 'ref', 'invoice_origin', 'payment_reference')])
        domain = expression.AND([[('move_type', 'in', ['out_invoice', 'out_refund'])], lookup])
        matches = self.search(domain, limit=2)
        if not matches:
            raise UserError(_('No invoice found for “%s” in your allowed companies and branches. Check the number or branch reference.', query))
        action = self.env['ir.actions.actions']._for_xml_id('tradeline_eta_reliability.action_eta_all_invoices')
        title = _('Invoice search: %s', query)
        action.update(name=title, display_name=title, domain=domain,
            context={'create': False, 'group_by': []})
        if len(matches) == 1:
            action.update(view_mode='form', views=[(False, 'form')], res_id=matches.id)
        return action

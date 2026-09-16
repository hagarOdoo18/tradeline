# -*- coding: utf-8 -*-
from odoo import http

import json

class tradeline_customer(http.Controller):

    @http.route('/mall_invoice',  type='json', auth="none",csrf=False)
    def get_mall_invoices ( self,  **kw) :

        try:
            if int(kw['branch'] )in [28,113] :
                invoices = http.request.env['config.day.line'].sudo().search([('user_id','=',int(kw['branch'])),('date','>=',kw['date_from']),('date','<=',kw['date_to'])])
                if not invoices:
                    return  json.dumps(str({
                        'code' : 300,
                        'state':'error',
                        'massage':'Not Found',

                    }))
                object =[]
                for invoice in invoices:
                    object.append({
                        'date': str(invoice.date),
                        'invoice_number': invoice.invoices_number,
                        'branch': invoice.user_id.name,
                        'untaxed_amount':invoice.new_untaxed_amount,
                        'tax_amount':invoice.new_tax_amount,
                        'total':invoice.new_total_amount,
                    })
                return  json.dumps(str({'code':200,
                            'state' : 'done',
                            'invoices':object
                            }))
            else:
                return json.dumps(str({'code': 300,
                                       'state': 'error', 'massage':'Not Allowed'}))
        except :
            return  json.dumps(str ({'code' : 400,
                        'state': 'error'}))

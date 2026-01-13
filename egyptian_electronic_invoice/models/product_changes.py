# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import models, fields, api


class ProductTemplate(models.Model):
    _inherit = 'product.template'
    # TODO: hs codes for testing
    # EG-535904355-99999 -> EGC error
    # EG-506507882-123 -> EGC
    # 99999999 -> GS1 -> or from website of GS1: https://www.gs1.org/services/gpc-browser
    hs_code = fields.Char(string="HS Code", help="Standardized code for international shipping and goods declaration."
                                                 " At the moment, only used for the FedEx shipping provider.")
    hs_description = fields.Char(string="HS Description", help="Taxpayer System HS Description.")
    hs_type = fields.Selection([('EGS', 'EGS'), ('GS1', 'GS1')], "HS Type", default="GS1",
                               help="Taxpayer System HS Type.")


class ProductProductInherit(models.Model):
    _inherit = 'product.product'
    
    hs_code = fields.Char(string="HS Code", help="Standardized code for international shipping and goods declaration."
                                                 " At the moment, only used for the FedEx shipping provider.")
    hs_description = fields.Char(string="HS Description", help="Taxpayer System HS Description.")
    hs_type = fields.Selection([('EGS', 'EGS'), ('GS1', 'GS1')], "HS Type", default="GS1",
                               help="Taxpayer System HS Type.")
    
    @api.model
    def create(self, vals):
        """
        load codes on template by default
        :param vals:
        :return:
        """
        product = super(ProductProductInherit, self).create(vals)
        if product.product_tmpl_id.hs_code and not product.hs_code:
            product.write({
                'hs_code': product.product_tmpl_id.hs_code,
                'hs_description': product.product_tmpl_id.hs_description,
                'hs_type': product.product_tmpl_id.hs_type,
            })
        return product
    

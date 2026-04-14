# -*- coding: utf-8 -*-
################################################################################
#
#    Cybrosys Technologies Pvt. Ltd.
#
#    Copyright (C) 2025-TODAY Cybrosys Technologies(<https://www.cybrosys.com>).
#    Author: Cybrosys Techno Solutions (Contact : odoo@cybrosys.com)
#
#    This program is under the terms of the Odoo Proprietary License v1.0
#    (OPL-1)
#    It is forbidden to publish, distribute, sublicense, or sell copies of the
#    Software or modified copies of the Software.
#
#    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
#    IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
#    DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
#    OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE
#    USE OR OTHER DEALINGS IN THE SOFTWARE.
#
################################################################################
from odoo import fields, models


class SaleOrderLine(models.Model):
    """Class for inherited model sale.order.line"""
    _inherit = 'sale.order.line'

    is_refund_line = fields.Boolean(string='Is Refund Line',
                                    readonly=True,
                                    help='Will be true if the order line is'
                                         ' a refund line')
    shopify_line_ref = fields.Char(string='Shopify Line Id', readonly=True,
                                   help='Line id in shopify')
    shopify_instance_id = fields.Many2one('shopify.configuration',
                                          string="Shopify Instance",
                                          help='Shopify instance id of '
                                               'sale order line')
    shopify_taxable = fields.Boolean(string='Line Item Taxable',
                                     help='Line item is taxable in shopify.')
    shopify_tax_amount = fields.Float(string='Shopify Tax Amount',
                                      help='Tax amount in shopify')
    shopify_discount_amount = fields.Float(string='Shopify Discount Amount',
                                           help='Discount amount in shopify')
    shopify_line_item_discount = fields.Float(string='Line Item Discount',
                                              help='Discount of line item '
                                                   'in shopify.')
    shopify_discount_code = fields.Char(string='Shopify Discount Code',
                                        help='Discount code in shopify.')

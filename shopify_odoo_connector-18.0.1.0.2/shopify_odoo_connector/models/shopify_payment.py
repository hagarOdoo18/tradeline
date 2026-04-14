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


class ShopifyPayment(models.Model):
    """Class for model shopify payment"""
    _name = 'shopify.payment'
    _description = 'Shopify Payments'

    shopify_order_ref = fields.Char(string='Shopify Order Id', readonly=True,
                                    help='Id of shopify order')
    payment_status = fields.Selection([('paid', 'Paid'),
                                       ('unpaid', 'Unpaid'),
                                       ('partially_paid', 'Partially Paid'),
                                       ('refunded', 'Refunded'), (
                                           'partially_refunded',
                                           'Partially Refunded')],
                                      string='Payment Status',
                                      help='Status of payment')
    company_id = fields.Many2one('res.company', string='Company',
                                 help='Company id')
    shopify_instance_id = fields.Many2one('shopify.configuration',
                                          string='Shopify Instance',
                                          help='Id of shopify instance')

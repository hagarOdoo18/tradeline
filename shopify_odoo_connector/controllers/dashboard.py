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
from odoo import http
from odoo.http import request


class Dashboard(http.Controller):
    """
    HTTP controller for Shopify dashboard analytics.
    """

    @http.route(['/dashboard'], type="json", auth="public")
    def dashboard(self, **kw):
        """
        Returns a list of Shopify configuration instances with related stats.
        """
        configs = request.env['shopify.configuration'].search([])
        return [{
            'id': config.id,
            'instance': config.name,
            'order': config.order_count,
            'customer': config.customer_count,
            'product': config.product_count,
            'consumer_key': config.consumer_key,
        } for config in configs]

    @http.route(['/total_dashboard'], type="json", auth="public")
    def total_dashboard(self, **kw):
        """
        Returns total count of synced orders, customers, and products.
        """
        return [{
            'order': request.env['sale.order'].search_count(
                [('shopify_sync_ids', '!=', False)]
            ),
            'customer': request.env['res.partner'].search_count(
                [('shopify_sync_ids', '!=', False)]
            ),
            'product': request.env['product.template'].search_count(
                [('shopify_sync_ids', '!=', False)]
            )
        }]

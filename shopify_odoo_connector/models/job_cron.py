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
import logging
from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class JobCron(models.Model):
    """ Class for recording jobs to be done to sync shopify and odoo

        Methods:
            _do_job(self):cron function to perform job  created in specific
            interval
            _refresh_shopify_tokens(self): scheduled action to proactively
            refresh access tokens for all active shopify instances
    """
    _name = 'job.cron'
    _description = 'Cron job '
    _rec_name = "model_id"

    model_id = fields.Many2one('ir.model', string='Model',
                               help="Model where the function written")
    instance_id = fields.Many2one('shopify.configuration',
                                  string='Instance',
                                  help="Instance Id on which have to "
                                       "sync the record")
    function = fields.Char(string="Function", help="Function to be performed")
    data = fields.Json(string="Data", help="Data, arguments for the function")
    wizard = fields.Integer(string="Wizard Id", help="Current Wizards Id")
    state = fields.Selection([('pending', 'Pending'), ('done', 'Done'),
                              ('failed', 'Failed')],
                             string='State', default='pending', readonly=True,
                             help="Status of record")

    @api.model
    def _do_job(self):
        """Method to do cron jobs for exporting and importing data."""
        job = self.env['job.cron'].sudo().search([('state', '=', 'pending')],
                                                 order='id asc', limit=1)
        if job:
            model = self.env[job.model_id.model].sudo().search([])
            if job.function == "import_products_from_shopify":
                try:
                    model.import_products_from_shopify(job.data,
                                                       job.instance_id)
                    job.state = "done"
                except Exception:
                    _logger.error('Some error has been occurred in the '
                                  'processing of function:'
                                  'import_products_from_shopify')
                    job.state = "failed"
            if job.function == "export_products_to_shopify":
                try:
                    model.export_products_to_shopify(job.data, job.instance_id)
                    job.state = "done"
                except Exception:
                    _logger.error(
                        'Some error has been occurred in the processing'
                        ' of function:export_products_to_shopify')
                    job.state = "failed"
            if job.function == "export_partners_to_shopify":
                try:
                    model.export_partners_to_shopify(job.data, job.instance_id)
                    job.state = "done"
                except Exception:
                    _logger.error(
                        'Some error has been occurred in the processing'
                        ' of function:export_partners_to_shopify')
                    job.state = "failed"
            if job.function == "import_customers_from_shopify":
                try:
                    model.import_customers_from_shopify(job.data,
                                                        job.instance_id)
                    job.state = "done"
                except Exception:
                    job.state = "failed"
            if job.function == "export_orders_to_shopify":
                try:
                    model.export_orders_to_shopify(job.data, job.instance_id)
                    job.state = "done"
                except Exception:
                    job.state = "failed"
            if job.function == "import_confirmed_orders_from_shopify":
                try:
                    model.import_confirmed_orders_from_shopify(job.data,
                                                               job.instance_id,
                                                               job.wizard)
                    job.state = "done"
                except Exception:
                    job.state = "failed"
            if job.function == "import_draft_orders_from_shopify":
                try:
                    model.import_draft_orders_from_shopify(job.data,
                                                           job.instance_id)
                    job.state = "done"
                except Exception as e:
                    job.state = "failed"

    @api.model
    def _refresh_shopify_tokens(self):
        """Scheduled action to proactively refresh Shopify access tokens for
        all active connected instances before they expire."""
        instances = self.env['shopify.configuration'].sudo().search([
            ('active', '=', True),
            ('state', '=', 'sync'),
            ('consumer_key', '!=', False),
            ('consumer_secret', '!=', False),
        ])
        for instance in instances:
            try:
                instance._fetch_new_access_token()
            except Exception as e:
                _logger.error(
                    'Failed to refresh Shopify access token for '
                    'instance %s: %s', instance.name, str(e))
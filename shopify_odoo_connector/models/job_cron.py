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
import time
from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# How long one _do_job tick may keep draining the queue. The cron fires every
# minute, so staying under 60s keeps at most one tick running at a time.
JOB_BUDGET_SECONDS = 50


class JobCron(models.Model):
    """ Class for recording jobs to be done to sync shopify and odoo

        Methods:
            _do_job(self):cron function to perform job  created in specific
            interval
            _process(self): run one queued job and set its state
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
        """Method to do cron jobs for exporting and importing data.

        Drains the pending queue for up to JOB_BUDGET_SECONDS instead of
        running a single job per tick. Processing one job per minute could
        never keep up with a producer that queues one batch per 20 records —
        the backlog only grew. Each job is committed on its own, so a failure
        never rolls back the jobs already done in this tick.
        """
        start = time.monotonic()
        processed = 0
        while time.monotonic() - start < JOB_BUDGET_SECONDS:
            job = self.env['job.cron'].sudo().search(
                [('state', '=', 'pending')], order='id asc', limit=1)
            if not job:
                break
            job._process()
            # keep each job's outcome even if a later one blows up, and
            # release the row locks this job took
            self.env.cr.commit()
            processed += 1
        if processed:
            _logger.info('Shopify job queue: processed %d job(s) in %.1fs',
                         processed, time.monotonic() - start)

    def _process(self):
        """Run this single queued job and record its outcome."""
        self.ensure_one()
        # Resolving the model must not escape this method: an empty model_id,
        # or one naming a model that left the registry, would raise before the
        # row is marked failed, and the drain loop would pick the very same
        # row on every tick and never get past it.
        try:
            model = self.env[self.model_id.model].sudo().search([])
        except (KeyError, ValueError):
            _logger.error('job.cron %s points at an unusable model (%s)',
                          self.id, self.model_id.model or self.model_id)
            self.state = 'failed'
            return
        handlers = {
            'import_products_from_shopify':
                lambda: model.import_products_from_shopify(
                    self.data, self.instance_id),
            'export_products_to_shopify':
                lambda: model.export_products_to_shopify(
                    self.data, self.instance_id),
            'export_inventory_to_shopify':
                lambda: model.export_inventory_to_shopify(
                    self.data, self.instance_id),
            'export_pricing_to_shopify':
                lambda: model.export_pricing_to_shopify(
                    self.data, self.instance_id),
            'export_partners_to_shopify':
                lambda: model.export_partners_to_shopify(
                    self.data, self.instance_id),
            'import_customers_from_shopify':
                lambda: model.import_customers_from_shopify(
                    self.data, self.instance_id),
            'export_orders_to_shopify':
                lambda: model.export_orders_to_shopify(
                    self.data, self.instance_id),
            'import_confirmed_orders_from_shopify':
                lambda: model.import_confirmed_orders_from_shopify(
                    self.data, self.instance_id, self.wizard),
            'import_draft_orders_from_shopify':
                lambda: model.import_draft_orders_from_shopify(
                    self.data, self.instance_id),
        }
        handler = handlers.get(self.function)
        if not handler:
            # never leave it pending: the drain loop would pick the same row
            # again and spin on it for the whole budget
            _logger.error('Unknown job.cron function: %s (job %s)',
                          self.function, self.id)
            self.state = 'failed'
            return
        try:
            handler()
            self.state = 'done'
        except Exception:
            _logger.exception(
                'Some error has been occurred in the processing of function: '
                '%s (job %s)', self.function, self.id)
            self.env.cr.rollback()
            self.state = 'failed'

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
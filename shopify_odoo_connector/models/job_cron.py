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
import random
import time

import psycopg2
from psycopg2 import errorcodes, errors as pg_errors

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# How long one _do_job tick may keep draining the queue, overridable per
# database with the system parameter
# 'shopify_odoo_connector.job_budget_seconds' (see _job_budget_seconds).
#
# The budget is only checked *between* jobs, and the inventory producer splits
# a pass into at most INVENTORY_MAX_JOBS (5) large jobs - 600 groups each at a
# 3000-group catalogue. A budget shorter than one job's run therefore means
# one job per tick, and five jobs take five minutes of waiting whatever the
# work itself costs. That is what 50s did here.
#
# Longer than the 1-minute cron interval on purpose: ir.cron holds a row lock
# on the cron record (SELECT ... FOR UPDATE NOWAIT) for the whole callback, so
# a tick that outlives the interval makes the next trigger skip rather than
# run beside it.
JOB_BUDGET_SECONDS = 240

# Postgres raises these when two transactions wrote the same row and this one
# lost the race. Nothing is wrong with the job itself: the same work replayed
# against a fresh snapshot normally goes through. Odoo retries an HTTP request
# that hits one of these (service.model.PG_CONCURRENCY_ERRORS_TO_RETRY); a
# cron callback gets no such treatment and has to retry on its own.
CONCURRENCY_ERRORCODES = frozenset((
    errorcodes.SERIALIZATION_FAILURE,
    errorcodes.DEADLOCK_DETECTED,
    errorcodes.LOCK_NOT_AVAILABLE,
))
# Attempts per job, and the base delay of the jittered back-off between them.
MAX_ATTEMPTS = 4
RETRY_BASE_DELAY = 0.5


def _is_concurrency_error(error):
    """True for the transient "something else got there first" PG errors.

    Matched on both the exception class and the Postgres error code:
    SerializationFailure and DeadlockDetected subclass TransactionRollback,
    LockNotAvailable does not, and an exception built by hand (a test) has no
    pgcode at all.
    """
    return (isinstance(error, pg_errors.TransactionRollback)
            or (isinstance(error, psycopg2.OperationalError)
                and error.pgcode in CONCURRENCY_ERRORCODES))


class JobCron(models.Model):
    """ Class for recording jobs to be done to sync shopify and odoo

        Methods:
            _do_job(self):cron function to perform job  created in specific
            interval
            _process(self): run one queued job and set its state
            _run_job(self, job_id): run one job in a transaction of its own,
            replaying it if it loses a row race
            _claim(self, job_id): take the row lock on a still-pending job
            _next_pending_id(self, after_id): next pending job of this tick
            _mark_failed(self, job_id): retire a job in a clean transaction
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

        Drains the pending queue for as long as :meth:`_job_budget_seconds`
        allows instead of running a single job per tick. Processing one job
        per minute could never keep up with a producer that queues a batch
        per 20 records — the backlog only grew.

        Each job runs, and is committed, in a transaction of its own, so a
        failure never rolls back the jobs already done in this tick. A job
        that loses a row race against one of the other Shopify crons (five of
        them run on minute or 5-minute schedules) or against an HTTP worker
        is replayed instead of retired.

        Nothing is allowed to escape this method: an exception out of a cron
        callback is what turned the whole tick into a failed server action
        and left the rest of the queue waiting for the next minute.
        """
        start = time.monotonic()
        budget = self._job_budget_seconds()
        processed = failed = 0
        after_id = 0
        while time.monotonic() - start < budget:
            job_id = self._next_pending_id(after_id)
            if not job_id:
                break
            # Advance past this row whatever becomes of it. A job left
            # pending (claimed by another runner, or still losing row races)
            # must not be offered to this tick again, or the loop would spin
            # on the same row for the whole budget.
            after_id = job_id
            outcome = self._run_job(job_id)
            if outcome == 'done':
                processed += 1
            elif outcome == 'failed':
                failed += 1
            elif outcome == 'conflict':
                # Whatever this queue writes is hot right now. Stop and let
                # the next tick have a go; leaning on those rows harder only
                # adds to the contention.
                _logger.warning(
                    'Shopify job queue: job %s still blocked by concurrent '
                    'updates after %d attempts, left pending for the next '
                    'run', job_id, MAX_ATTEMPTS)
                break
        elapsed = time.monotonic() - start
        if processed or failed:
            _logger.info(
                'Shopify job queue: %d job(s) done, %d failed in %.1fs '
                '(budget %.0fs%s)', processed, failed, elapsed, budget,
                ', spent' if elapsed >= budget else '')

    @api.model
    def _job_budget_seconds(self):
        """How long one tick may keep draining, in seconds.

        Tunable per database with the system parameter
        'shopify_odoo_connector.job_budget_seconds'; a missing or unusable
        value falls back to JOB_BUDGET_SECONDS. It has to exceed the run time
        of a single job, or the queue advances one job per tick - see the
        note on the constant.
        """
        try:
            budget = float(self.env['ir.config_parameter'].sudo().get_param(
                'shopify_odoo_connector.job_budget_seconds',
                JOB_BUDGET_SECONDS))
        except (TypeError, ValueError):
            budget = JOB_BUDGET_SECONDS
        return budget if budget > 0 else JOB_BUDGET_SECONDS

    @api.model
    def _next_pending_id(self, after_id=0):
        """Id of the next pending job after `after_id`, or None."""
        job = self.sudo().search(
            [('state', '=', 'pending'), ('id', '>', after_id)],
            order='id asc', limit=1)
        return job.id or None

    @api.model
    def _claim(self, job_id):
        """Take the row lock on `job_id`, if it is still pending and free.

        `SKIP LOCKED` rather than a plain `FOR UPDATE`: when another runner
        already holds the row (a second cron worker, or Run Manually from the
        UI) this tick moves on instead of queueing behind it and then doing
        the same Shopify work a second time. The lock is held until the
        caller commits or rolls back, so the job cannot be claimed twice
        while it runs.
        """
        self.env.cr.execute(
            """SELECT id FROM job_cron
                 WHERE id = %s AND state = 'pending'
                   FOR UPDATE SKIP LOCKED""", (job_id,))
        return bool(self.env.cr.fetchone())

    @api.model
    def _run_job(self, job_id):
        """Run one job in a transaction of its own and commit it.

        Returns 'done' or 'failed' (the state recorded for the job),
        'skipped' (another runner holds it, or it is no longer pending), or
        'conflict' (it kept losing row races and is left pending).

        A lost row race is not the job's fault, so the handler is replayed
        against a fresh snapshot rather than the row being burned — the same
        treatment Odoo gives an HTTP request that hits the error. The replay
        is what the old code did too, only a minute later and with a failed
        server action in between: a handler that reaches Shopify before the
        commit conflicts may therefore send its writes twice, which the
        inventory and pricing pushes are built for (every quantity is
        written unconditionally) and the imports are idempotent about.
        """
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                if not self._claim(job_id):
                    self.env.cr.rollback()
                    return 'skipped'
                # sudo: the handlers write products, partners, orders and
                # stock. The old drain loop searched with sudo() and so ran
                # them as superuser; this cron's user_id is not root.
                state = self.browse(job_id).sudo()._process()
                # keep this job's outcome even if a later one blows up, and
                # release the row locks it took
                self.env.cr.commit()
                return state
            except Exception as error:
                # The transaction is aborted either way: nothing can be read
                # or written on this cursor again until it is rolled back.
                self.env.cr.rollback()
                if not _is_concurrency_error(error):
                    # _process() records its own handler failures, so this is
                    # the commit (or _process itself) breaking. Retire the
                    # row: left pending it would be picked up and broken on
                    # again every single minute.
                    _logger.exception(
                        'Shopify job %s could not be stored', job_id)
                    self._mark_failed(job_id)
                    return 'failed'
                if attempt == MAX_ATTEMPTS:
                    return 'conflict'
                # Jittered, so the replay does not come back in lockstep
                # with whatever it is racing.
                delay = RETRY_BASE_DELAY * attempt * (1 + random.random())
                _logger.info(
                    'Shopify job %s hit a concurrent update, replaying in '
                    '%.1fs (attempt %d/%d)', job_id, delay, attempt,
                    MAX_ATTEMPTS)
                time.sleep(delay)
        return 'conflict'

    @api.model
    def _mark_failed(self, job_id):
        """Record a job as failed in a transaction of its own.

        Only ever called just after a rollback, so this write is the only one
        in flight and cannot be dragged down by whatever the job had pending.
        """
        try:
            self.browse(job_id).sudo().write({'state': 'failed'})
            self.env.cr.commit()
        except Exception:
            self.env.cr.rollback()
            _logger.exception('Shopify job %s could not be marked failed',
                              job_id)

    def _process(self):
        """Run this single queued job and record its outcome.

        Returns the state it wrote, 'done' or 'failed', so the caller can
        count outcomes without reading the row back. A lost row race is
        re-raised rather than recorded: see :meth:`_run_job`.
        """
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
            return 'failed'
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
            return 'failed'
        try:
            handler()
            self.state = 'done'
            return 'done'
        except Exception as error:
            if _is_concurrency_error(error):
                # Transient. Marking this failed would retire a job whose
                # only problem is that something else wrote one of the same
                # rows first; the caller replays it instead.
                raise
            _logger.exception(
                'Some error has been occurred in the processing of function: '
                '%s (job %s)', self.function, self.id)
            self.env.cr.rollback()
            self.state = 'failed'
            return 'failed'

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
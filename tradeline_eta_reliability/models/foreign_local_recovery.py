"""Daily remediation: reclassify rejected foreign-customer invoices as local,
then let the normal worker regenerate their ETA JSON from the corrected
customer and resubmit. Driven by the cron
``cron_eta_foreign_to_local`` -> ``model._cron_retry_foreign_as_local(50)``.
"""
import logging

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

_logger = logging.getLogger(__name__)

FOREIGN_LOCAL_DAILY_LIMIT = 50


class EtaForeignLocalRecovery(models.Model):
    _inherit = 'tradeline.eta.job'

    @api.model
    def _foreign_local_enabled_database(self):
        """True only on the activated production database, so a staging copy
        never submits with production credentials (same guard as the worker)."""
        self.env['ir.config_parameter'].sudo().flush_model(['key', 'value'])
        self.env.cr.execute(
            "SELECT value FROM ir_config_parameter "
            "WHERE key='tradeline_eta_reliability.enabled_database'")
        row = self.env.cr.fetchone()
        return bool(row and row[0] == self.env.cr.dbname)

    @api.model
    def _foreign_local_candidate_domain(self):
        """Rejected customer invoices/credit notes whose customer is still
        classified as a foreigner (ETA receiver type 'F')."""
        return [
            ('move_id.e_invoice_status', '=', 'Error'),
            ('move_id.state', '=', 'posted'),
            ('move_id.move_type', 'in', ('out_invoice', 'out_refund')),
            ('move_id.partner_id.mobile_type', '=', 'foreigner'),
        ]

    @api.model
    def _cron_retry_foreign_as_local(self, limit=FOREIGN_LOCAL_DAILY_LIMIT):
        """Convert up to ``limit`` rejected foreign-customer invoices to local
        and requeue them for ETA.

        Each invoice is handled in its own savepoint and committed, so one
        failure never rolls back the rest. Only the 11-digit local-mobile check
        is waived (via the ``eta_foreign_to_local_cron`` context); the national
        ID / VAT length rule stays in force, so a customer that cannot become a
        valid local record raises and is skipped rather than force-sent.

        Already-submitted invoices (they carry an ETA UUID) go through the
        module's safe corrected-document path, which verifies the previous
        document against ETA before clearing its receipt, so an accepted
        document is never duplicated. Never-submitted invoices are simply reset
        to ``queued``; the worker then rebuilds their JSON from the now-local
        customer.
        """
        if 'mobile_type' not in self.env['res.partner']._fields:
            return 0
        if not self._foreign_local_enabled_database():
            return 0

        jobs = self.sudo().search(
            self._foreign_local_candidate_domain(), order='id', limit=limit)
        requeued = 0
        for job in jobs:
            move = job.move_id
            partner = move.partner_id
            try:
                with self.env.cr.savepoint():
                    # Waive only the 11-digit local-mobile rule. Flushing here
                    # makes the remaining partner constraints (14-digit national
                    # ID / 9-digit VAT, unique mobile) raise inside the
                    # savepoint so an invalid conversion is skipped, not sent.
                    partner.with_context(
                        eta_foreign_to_local_cron=True).write({'mobile_type': 'local'})
                    partner.flush_recordset()

                    if move.e_invoice_uuid:
                        # Verify against ETA before clearing the receipt.
                        move.sudo()._eta_queue_corrected_document()
                    else:
                        # Never received a receipt: rebuild on the next cycle.
                        job.write({
                            'state': 'queued', 'payload': False, 'attempted_at': False,
                            'submission_uuid': False, 'next_attempt': False,
                            'safe_to_retry': False, 'first_submission_candidate': False,
                            'last_error': False, 'failures': 0,
                        })
            except (UserError, ValidationError, AccessError) as exc:
                _logger.info(
                    'ETA foreign->local skipped %s (customer %s): %s',
                    move.display_name, partner.display_name, exc)
                continue
            except Exception:
                _logger.exception('ETA foreign->local failed for %s', move.display_name)
                continue

            requeued += 1
            self.env.cr.commit()

        if requeued:
            cron = self.env.ref(
                'tradeline_eta_reliability.cron_eta_queue', raise_if_not_found=False)
            if cron and cron.sudo().active:
                cron.sudo()._trigger()
        _logger.info(
            'ETA foreign->local: requeued %s of %s candidate(s).', requeued, len(jobs))
        return requeued

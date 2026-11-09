"""Daily remediation applied directly on the invoice (no ETA job queue):
reclassify error-status foreign-customer invoices as local, regenerate their
ETA JSON from the corrected customer and resubmit directly to ETA.

Driven by the cron ``cron_eta_foreign_to_local`` ->
``model._cron_retry_foreign_as_local(50)``.
"""
import logging

from odoo import api, models
from odoo.exceptions import AccessError, UserError, ValidationError

_logger = logging.getLogger(__name__)

FOREIGN_LOCAL_DAILY_LIMIT = 50


class EtaForeignLocalRecovery(models.Model):
    _inherit = 'account.move'

    @api.model
    def _foreign_local_enabled_database(self):
        """True only on the activated production database, so a staging copy
        never submits with production credentials."""
        self.env['ir.config_parameter'].sudo().flush_model(['key', 'value'])
        self.env.cr.execute(
            "SELECT value FROM ir_config_parameter "
            "WHERE key='tradeline_eta_reliability.enabled_database'")
        row = self.env.cr.fetchone()
        return bool(row and row[0] == self.env.cr.dbname)

    @api.model
    def _foreign_local_candidate_domain(self):
        """Error-status customer invoices/credit notes whose customer is still
        classified as a foreigner (ETA receiver type 'F')."""
        return [
            ('e_invoice_status', '=', 'Error'),
            ('state', '=', 'posted'),
            ('move_type', 'in', ('out_invoice', 'out_refund')),
            ('partner_id.mobile_type', '=', 'foreigner'),
        ]

    @api.model
    def _cron_retry_foreign_as_local(self, limit=FOREIGN_LOCAL_DAILY_LIMIT):
        """Convert up to ``limit`` error-status foreign-customer invoices to
        local and resubmit them directly to ETA.

        Each invoice is handled in its own savepoint and committed, so one
        failure never rolls back the rest. Only the 11-digit local-mobile check
        is waived (via the ``eta_foreign_to_local_cron`` context); the national
        ID / VAT length rule stays in force, so a customer that cannot become a
        valid local record raises and is skipped rather than force-sent.

        The resend regenerates the ETA JSON from the now-local customer and
        posts it straight to ETA; the reliability job queue is not used.
        """
        if 'mobile_type' not in self.env['res.partner']._fields:
            return 0
        if not self._foreign_local_enabled_database():
            return 0

        moves = self.sudo().search(
            self._foreign_local_candidate_domain(), order='id', limit=limit)
        done = 0
        for move in moves:
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
                    # Regenerates the JSON from the local customer and submits
                    # directly to ETA (action_send_electronic_invoice calls
                    # action_generate_eta_json internally).
                    move.sudo().action_send_electronic_invoice()
            except (UserError, ValidationError, AccessError) as exc:
                _logger.info(
                    'ETA foreign->local skipped %s (customer %s): %s',
                    move.display_name, partner.display_name, exc)
                continue
            except Exception:
                _logger.exception('ETA foreign->local failed for %s', move.display_name)
                continue
            done += 1
            self.env.cr.commit()

        _logger.info(
            'ETA foreign->local: resubmitted %s of %s candidate(s).', done, len(moves))
        return done

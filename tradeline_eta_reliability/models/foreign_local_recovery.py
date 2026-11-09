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
        failure never rolls back the rest. The ``eta_foreign_to_local_cron``
        context waives both the 11-digit local-mobile check and the national
        ID / VAT length rule, so the customer can be converted even without a
        14-digit national ID. The remaining guards still apply and skip an
        invoice rather than send bad data: the unique/required mobile checks,
        and ETA's own rule that a domestic individual invoice of EGP 25,000 or
        more must carry a valid 14-digit national ID (raised at JSON build).

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
            # Carry the cron flag (and sudo) through the WHOLE operation. The
            # partner constraints are validated at flush time against the env
            # that triggers the flush, so the write, its validation flush and
            # the send must all run under this context or the waiver is lost.
            cmove = move.with_context(eta_foreign_to_local_cron=True).sudo()
            partner = cmove.partner_id
            try:
                with self.env.cr.savepoint():
                    # Convert to local. Under this flagged, superuser context the
                    # 11-digit mobile and national ID / VAT length checks are
                    # waived; the flush validates the change here, still under
                    # the flag.
                    partner.write({'mobile_type': 'local'})
                    partner.flush_recordset()
                    # Regenerate the JSON from the now-local customer and submit
                    # directly to ETA (action_send_electronic_invoice calls
                    # action_generate_eta_json internally). Skips still happen
                    # via ETA's own send-time rules (e.g. missing national ID on
                    # a large individual invoice), which raise and are caught.
                    cmove.action_send_electronic_invoice()
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

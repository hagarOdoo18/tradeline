"""Daily remediation applied directly on the invoice (no ETA job queue):
reclassify error-status foreign-customer invoices as local, regenerate their
ETA JSON from the corrected customer and resubmit directly to ETA.

Driven by the cron ``cron_eta_foreign_to_local`` ->
``model._cron_retry_foreign_as_local(50)``.
"""
import logging

from odoo import api, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .eta_monitoring import error_details

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
        invoice rather than send bad data: the unique/required mobile checks.
        ETA's own rule that a domestic individual invoice of EGP 25,000 or more
        must carry a valid 14-digit national ID (raised at JSON build) is
        handled specially: the customer is kept a foreigner (never converted to
        local) and their receiver id is stamped 'NA' so the invoice can go out
        under the foreign receiver type instead.

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
            # ETA rejected an earlier submission for the customer's foreign
            # receiver details. Correct them in place and keep the customer a
            # foreigner (do not convert to local): CF320 means the country or
            # classification does not match, so set the country to United
            # States; a missing/invalid identification (NI*, CF305, SF307)
            # means the receiver id is unacceptable, so stamp it 'NA'. Both can
            # apply at once, so build a single write, then resend the invoice as
            # a consistent foreign document in the same pass.
            recorded = ' '.join(e for e in cmove.eta_job_ids.mapped('last_error') if e) \
                or (cmove.e_invoice_issue or '')
            codes = error_details(recorded)[1]
            foreign_fix = {}
            if 'CF320' in codes:
                foreign_fix['country_id'] = self.env.ref('base.us').id
            if any(code.startswith('NI') for code in codes) or codes & {'CF305', 'SF307'}:
                foreign_fix['vat'] = 'NA'
            if foreign_fix:
                try:
                    partner.sudo().with_context(eta_foreign_to_local_cron=True).write(foreign_fix)
                    self.env.cr.commit()
                except Exception:
                    self.env.cr.rollback()
                    _logger.exception(
                        'ETA foreign->local: could not apply foreign receiver fix %s on %s',
                        foreign_fix, partner.display_name)
                    continue
                _logger.info(
                    'ETA foreign->local: kept %s foreign and applied %s for %s',
                    partner.display_name, foreign_fix, move.display_name)
                # The receiver details are now corrected and committed; resend
                # the invoice under the foreign receiver type in this same pass.
                # Regenerate the JSON from the corrected (still foreign) customer
                # and post straight to ETA, mirroring the local path below.
                # Resending here is required: the fix write never changes the
                # invoice's recorded error codes, so a deferred "later pass"
                # recomputes the identical foreign_fix and continues forever
                # without ever resubmitting. A send failure leaves a consistent
                # corrected foreign customer, so the next pass simply retries.
                try:
                    with self.env.cr.savepoint():
                        cmove.action_send_electronic_invoice()
                except (UserError, ValidationError, AccessError) as exc:
                    _logger.info(
                        'ETA foreign->local resend skipped %s (customer %s): %s',
                        move.display_name, partner.display_name, exc)
                    continue
                except Exception:
                    _logger.exception(
                        'ETA foreign->local resend failed for %s', move.display_name)
                    continue
                done += 1
                self.env.cr.commit()
                continue
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
                if 'national id is required for domestic individual invoices' in str(exc).lower():
                    # ETA blocks a domestic individual invoice of EGP 25,000 or
                    # more without a valid national ID, so this customer cannot
                    # be converted to local. The failed local write has already
                    # rolled back with the savepoint, so the customer stays a
                    # foreigner; stamp the receiver id as 'NA' so the invoice can
                    # go out under the foreign receiver type instead of local.
                    try:
                        partner.invalidate_recordset()
                        partner.sudo().with_context(
                            eta_foreign_to_local_cron=True).write({'vat': 'NA'})
                        self.env.cr.commit()
                    except Exception:
                        self.env.cr.rollback()
                        _logger.exception(
                            'ETA foreign->local: could not set vat=NA on %s',
                            partner.display_name)
                    else:
                        _logger.info(
                            'ETA foreign->local: kept %s foreign and set vat=NA '
                            '(EGP 25,000 national ID rule) for %s',
                            partner.display_name, move.display_name)
                    continue
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

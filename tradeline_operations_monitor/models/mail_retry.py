"""Retry explicit temporary SMTP rejections through Odoo's normal mail queue."""
import re
from datetime import timedelta

from odoo import api, fields, models


RETRY_MINUTES = (5, 15, 60)
ERROR_LIMIT = 8000


def temporary_smtp_rejection(reason):
    """Require an SMTP response, never infer retry safety from arbitrary digits.

    Timeouts/disconnects have an uncertain delivery outcome; authentication,
    permanent rejections and mixed recipient responses need human review.
    """
    reason = str(reason or '')
    if re.search(r'SMTPAuthenticationError|SMTPRecipientsRefused', reason, re.I):
        return False
    codes = re.findall(
        r'(?:SMTP\w*Error\s*:\s*|^\s*)\(?\s*([45]\d{2})(?=[\s,):])', reason, re.M)
    # Odoo can persist str(SMTPAuthenticationError) without its class name.
    # 454 is a temporary authentication/TLS failure, not a retryable message.
    return bool(codes) and all(code.startswith('4') and code != '454' for code in codes)


def smtp_excerpt(reason):
    reason = str(reason or '')
    if len(reason) > ERROR_LIMIT:
        return reason[:ERROR_LIMIT] + '\n[Error truncated. Open the original email for the full error.]'
    return reason


class MailTemporaryRetry(models.Model):
    _inherit = 'mail.mail'

    operations_retry_count = fields.Integer(default=0, readonly=True, copy=False, groups='base.group_system')
    operations_retry_next_at = fields.Datetime(readonly=True, copy=False, index=True, groups='base.group_system')
    operations_retry_note = fields.Char(readonly=True, copy=False, groups='base.group_system')
    operations_last_smtp_error = fields.Text(readonly=True, copy=False, groups='base.group_system')

    def _postprocess_sent_message(self, success_pids, failure_reason=False, failure_type=None):
        # This hook sees the final outcome, unlike write(state='exception'),
        # which Odoo also uses as a guard *before* attempting SMTP delivery.
        now = fields.Datetime.now()
        for mail in self.sudo():
            if mail.state == 'sent' and not failure_type:
                if mail.operations_retry_count or mail.operations_retry_next_at or mail.operations_last_smtp_error:
                    mail.write({'operations_retry_next_at': False, 'operations_retry_note': 'Sent'})
            elif mail.state == 'exception' and failure_type:
                reason = failure_reason or mail.failure_reason or ''
                values = {'operations_retry_next_at': False,
                          'operations_last_smtp_error': smtp_excerpt(reason)}
                if not temporary_smtp_rejection(reason):
                    values['operations_retry_note'] = 'Manual review required; no explicit temporary SMTP rejection.'
                elif success_pids or (mail.recipient_ids and (mail.email_to or mail.email_cc)):
                    # Resending the original mail would also resend to partners
                    # already accepted by SMTP earlier in this send attempt.
                    values['operations_retry_note'] = 'Partial delivery or mixed recipient groups; review recipients to avoid duplicate emails.'
                elif mail.operations_retry_count >= len(RETRY_MINUTES):
                    values['operations_retry_note'] = 'Automatic retries exhausted; manual review required.'
                else:
                    values.update(
                        operations_retry_next_at=now + timedelta(minutes=RETRY_MINUTES[mail.operations_retry_count]),
                        operations_retry_note='Automatic retry %s of %s scheduled.' % (
                            mail.operations_retry_count + 1, len(RETRY_MINUTES)))
                mail.write(values)
        return super()._postprocess_sent_message(
            success_pids, failure_reason=failure_reason, failure_type=failure_type)

    @api.model
    def _cron_retry_temporary_failures(self):
        """Reserve due retries once, without making SMTP calls in this job.

        Failed mails stay in exception until due, so neither the standard queue
        nor the alert sender can bypass the backoff. Never replay old failures
        that were not explicitly scheduled by the final-outcome hook.
        """
        Mail = self.sudo()
        Mail.flush_model(['state', 'operations_retry_next_at', 'operations_retry_count'])
        now = fields.Datetime.now()
        self.env.cr.execute('''
            SELECT id FROM mail_mail
             WHERE state = 'exception' AND operations_retry_next_at <= %s
               AND operations_retry_count < %s
             ORDER BY operations_retry_next_at, id
             LIMIT 50 FOR UPDATE SKIP LOCKED
        ''', [now, len(RETRY_MINUTES)])
        mails = Mail.browse([row[0] for row in self.env.cr.fetchall()])
        mails.invalidate_recordset()
        for mail in mails:
            mail.write({'state': 'outgoing', 'scheduled_date': now,
                        'operations_retry_count': mail.operations_retry_count + 1,
                        'operations_retry_next_at': False,
                        'operations_retry_note': 'Automatic retry queued for delivery.'})
        if mails:
            self.env.ref('mail.ir_cron_mail_scheduler_action')._trigger()
        return len(mails)

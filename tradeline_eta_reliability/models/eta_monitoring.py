"""Human-readable, read-only ETA diagnostics. No transport or submission actions."""
import json
import re
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.osv import expression


def error_details(message):
    """Unwrap both validationResults and prefixed submission rejection JSON."""
    raw = str(message or '').strip()
    codes = set(re.findall(r'''["'](?:errorCode|code)["']\s*:\s*["']([^"']+)["']''', raw))
    codes.update(re.findall(r'\b(?:CF\d+|NI\d+|SF\d+|CV\d+|DS\d+)\b', raw))
    # An invoice may itself be numbered 4043. Only a code field or the explicit
    # signer exception prefix identifies the numeric signature error.
    codes.update(re.findall(r'\b(4043)\s*:\s*Exception\b', raw))
    decoder = json.JSONDecoder()
    try:
        start = raw.index('{')
        data, _end = decoder.raw_decode(raw[start:])
        nodes = [data]
    except (ValueError, TypeError):
        nodes = []
        # The submission ledger intentionally caps error text. Recover complete
        # nested errors even when the outer authority JSON was truncated.
        for match in re.finditer(r'\{', raw):
            try:
                node, _end = decoder.raw_decode(raw[match.start():])
                nodes.append(node)
            except ValueError:
                continue
    lines = []

    def visit(node):
        if isinstance(node, list):
            for value in node:
                visit(value)
        elif isinstance(node, dict):
            code = node.get('errorCode') or node.get('code')
            if code:
                codes.add(str(code))
            before = len(lines)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    visit(value)
            text = node.get('error') or node.get('message')
            if isinstance(text, str) and text.strip() and len(lines) == before:
                target = node.get('propertyPath') or node.get('target') or node.get('propertyName')
                label = ' / '.join(str(v) for v in (code, target) if v)
                line = '%s: %s' % (label, text.strip()) if label else text.strip()
                if line not in lines:
                    lines.append(line)

    for node in nodes:
        visit(node)
    return ('; '.join(lines) or raw), codes


class EtaJobMonitoring(models.Model):
    _inherit = 'tradeline.eta.job'

    invoice_date = fields.Date(related='move_id.invoice_date', readonly=True)
    customer_id = fields.Many2one(related='move_id.partner_id', readonly=True)
    reason_summary = fields.Text(string='Why it is waiting / held', compute='_compute_monitoring')
    required_action = fields.Text(string='Next step', compute='_compute_monitoring')
    waiting_minutes = fields.Integer(string='Waiting (minutes)', compute='_compute_monitoring')
    needs_attention = fields.Boolean(compute='_compute_monitoring', search='_search_needs_attention')

    @api.model
    def _recorded_rejection_domain(self):
        return expression.AND([[('state', '=', 'review')], expression.OR([
            [('safe_to_retry', '=', True), ('last_error', '=like', 'ETA rejected the submission (%')],
            [('last_error', '=like', '{"status": "Invalid"%')],
            [('last_error', '=like', '{"status":"Invalid"%')],
        ])])

    def _has_recorded_rejection(self):
        self.ensure_one()
        raw = self.last_error or ''
        return (self.state == 'review' and (
            self.safe_to_retry and raw.startswith('ETA rejected the submission (')
            or raw.startswith(('{"status": "Invalid"', '{"status":"Invalid"'))))

    @api.model
    def _attention_domain(self, now=None):
        now = now or fields.Datetime.now(); cutoff = now - timedelta(minutes=15)
        return expression.OR([
            [('state', 'in', ['blocked', 'invalid'])],
            [('state', '=', 'unknown'), '|', ('duplicate_retry_at', '=', False), ('duplicate_retry_at', '<=', cutoff)],
            [('state', '=', 'review'), ('failures', '>', 0)],
            self._recorded_rejection_domain(),
            [('state', '=', 'queued'), ('create_date', '<=', cutoff),
             '|', ('next_attempt', '=', False), ('next_attempt', '<=', cutoff)],
            [('state', '=', 'accepted'), ('attempted_at', '<=', cutoff)],
            [('state', '=', 'accepted'), ('attempted_at', '=', False),
             ('move_id.e_invoice_date', '<=', cutoff)],
            [('state', '=', 'accepted'), ('attempted_at', '=', False),
             ('move_id.e_invoice_date', '=', False), ('create_date', '<=', cutoff)],
            [('state', '=', 'valid'), '|', ('last_error', 'ilike', 'issue date'),
             ('move_id.e_invoice_issue', 'ilike', 'issue date')],
        ])

    @api.model
    def _search_needs_attention(self, operator, value):
        if operator not in ('=', '!=') or value not in (True, False):
            raise ValueError('Use a boolean equality filter for ETA attention.')
        domain = self._attention_domain()
        return domain if bool(value) == (operator == '=') else ['!'] + expression.normalize_domain(domain)

    @api.depends('state', 'last_error', 'create_date', 'attempted_at', 'next_attempt',
                 'move_id.e_invoice_date', 'move_id.e_invoice_issue', 'issue_date_policy', 'duplicate_retry_at', 'safe_to_retry')
    def _compute_monitoring(self):
        now = fields.Datetime.now()
        cron = self.env.ref('tradeline_eta_reliability.cron_eta_queue', raise_if_not_found=False)
        cron = cron.sudo() if cron else cron
        enabled = (bool(cron and cron.active and not cron.failure_count)
                   and self.env['ir.config_parameter'].sudo().get_param(
                       'tradeline_eta_reliability.enabled_database') == self.env.cr.dbname)
        overdue = bool(enabled and cron.nextcall and cron.nextcall < now - timedelta(minutes=3))
        for job in self:
            anchor = (job.attempted_at or job.move_id.e_invoice_date or job.create_date
                      if job.state == 'accepted' else job.create_date)
            minutes = max(0, int((now - anchor).total_seconds() / 60)) if anchor else 0
            raw = job.last_error or ''
            date_review = job.state == 'valid' and 'issue date' in (
                raw + ' ' + (job.move_id.e_invoice_issue or '')).lower()
            if date_review and 'issue date' not in raw.lower():
                raw = job.move_id.e_invoice_issue
            detail, codes = error_details(raw)
            lower = detail.lower()
            actions = []
            if 'CF313' in codes or 'outside the configured eta submission window' in lower:
                actions.append(_('Current ETA issue-date treatment is approved. Correct any remaining reported errors before retrying.')
                               if job.issue_date_policy == 'current' else
                               _('Finance must arrange the approved ETA late-submission route. Keep the original invoice date.'))
            if 'DS301' in codes or 'duplicate' in lower:
                actions.append(_('Reconcile the referenced original ETA receipt before considering another submission.'))
            if (any(code.startswith('NI') for code in codes) or codes.intersection({'SF307', 'CF305'})
                    or any(word in lower for word in ('national id', 'receiver id', 'tax id'))):
                actions.append(_('Verify the customer ID and customer classification in the customer record.'))
            if 'CV302' in codes or any(word in lower for word in ('product', 'itemcode', 'ar_description')):
                actions.append(_('Correct the product details and verify its ETA-approved EGS/GS1 code.'))
            if '4043' in codes or any(word in lower for word in ('signer', 'signing', 'signature')):
                actions.append(_('Check the company signing service and certificate configuration.'))
            if any(word in lower for word in ('serialization', 'calculation review', 'five-decimal', 'tax allocations', 'posted exchange rate')):
                actions.append(_('IT must reconcile ETA calculations with the posted ledger. Preserve the accounting dates and amounts.'))
            if job.duplicate_retry_at:
                actions.append(_('Wait for the ETA Retry-After deadline; automatic receipt reconciliation will run before any retry.'))
            if job.state == 'valid':
                reason = detail if date_review else _('ETA approved this invoice.')
                action = (_('Finance must review the issue-date discrepancy against the existing approved receipt. Do not automatically cancel or resend it.')
                          if date_review else _('No submission action required.'))
            elif job.state == 'review':
                reason = detail or _('Historical receipt verification is queued; no new invoice has been sent.')
                if job._has_recorded_rejection():
                    action = ' '.join(actions + [_('A previous submission was rejected. Verify or correct the reported source fields; receipt verification must finish before any corrected submission.')])
                else:
                    action = (_('IT must restore the authority-history scan or resolve its communication error. Automatic retries continue.')
                              if job.failures else _('Automatic recovery checks existing ETA receipts, then submits only documents with a proven safe submission route.'))
            elif job.state == 'unknown' and job.duplicate_retry_at:
                reason = _('ETA duplicate-submission cooldown is being reconciled automatically after Retry-After.')
                action = _('The worker checks for an existing ETA receipt before retrying the retained signed request. No manual resubmission is needed.')
            elif job.state == 'unknown':
                reason = detail or _('The submission outcome is uncertain; a reliable ETA receipt has not been matched.')
                action = _('Reconcile the existing ETA outcome first. Automatic checks continue; do not blindly resend.')
            elif job.state == 'accepted':
                reason = (_('ETA received this invoice; status checking is delayed: %s', detail)
                          if detail else _('ETA received this invoice and has not yet returned a final validation result.'))
                action = _('Automatic status checks continue. Investigate prolonged delays; do not submit another copy.')
            elif job.state == 'queued':
                if detail:
                    reason = _('Automatic processing was deferred: %s', detail)
                    action = ' '.join(actions + [
                        _('The worker will retry automatically at the next retry time.') if enabled else
                        _('Restore the ETA scheduled worker before automatic retries can continue.')])
                elif not enabled:
                    reason = _('No submission attempt yet. The automatic ETA worker is disabled, failing or not enabled for this database.')
                    action = _('Restore the ETA scheduled worker and its production database activation. The invoice remains queued.')
                elif overdue:
                    reason = _('No submission attempt yet. The automatic ETA scheduled worker is overdue.')
                    action = _('Check scheduler availability and database delays. The invoice remains queued.')
                else:
                    reason = _('Waiting in the automatic submission queue; no submission attempt has been recorded yet.')
                    action = _('The enabled worker will process this invoice. Check queue progress if the delay continues.')
            else:
                reason = detail or (_('ETA marked this invoice invalid; detailed validation information needs review.')
                                    if job.state == 'invalid' else _('Processing is held for correction or review.'))
                action = ' '.join(actions) or _('Review the detailed error and correct the source issue before an accounting manager queues a verified retry.')
            job.reason_summary = reason
            job.required_action = action
            job.waiting_minutes = minutes if job.state != 'valid' else 0
            job.needs_attention = (job.state in ('blocked', 'invalid') or date_review
                                   or job.state == 'unknown' and (not job.duplicate_retry_at or job.duplicate_retry_at <= now - timedelta(minutes=15))
                                   or job.state == 'review' and job.failures > 0
                                   or job._has_recorded_rejection()
                                   or job.state == 'accepted' and minutes >= 15
                                   or job.state == 'queued' and minutes >= 15 and (not job.next_attempt or job.next_attempt <= now - timedelta(minutes=15)))

    def action_open_invoice(self):
        self.ensure_one()
        self.move_id.check_access('read')
        return {'type': 'ir.actions.act_window', 'name': _('Invoice'), 'res_model': 'account.move',
                'view_mode': 'form', 'res_id': self.move_id.id, 'target': 'current'}

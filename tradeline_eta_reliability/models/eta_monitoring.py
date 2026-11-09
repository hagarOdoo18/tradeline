"""Human-readable, read-only ETA diagnostics. No transport or submission actions."""
import json
import re
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.osv import expression
from .eta_job import DUPLICATE_REVIEW_PREFIX


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
    staff_summary = fields.Text(string='Issue / explanation', compute='_compute_monitoring')
    required_action = fields.Text(string='Next step', compute='_compute_monitoring')
    waiting_minutes = fields.Integer(string='Waiting (minutes)', compute='_compute_monitoring')
    needs_attention = fields.Boolean(compute='_compute_monitoring', search='_search_needs_attention')

    @api.model
    def _recorded_rejection_domain(self):
        return expression.AND([[('state', '=', 'review')], expression.OR([
            [('safe_to_retry', '=', True), ('last_error', '=like', 'ETA rejected the submission (%')],
            [('last_error', '=like', '{"status": "Invalid"%')],
            [('last_error', '=like', '{"status":"Invalid"%')],
            [('last_error', 'ilike', 'national ID')],
            [('last_error', 'ilike', 'receiver ID')],
            [('last_error', 'ilike', 'customer ID')],
            [('last_error', 'ilike', 'tax ID')],
            [('last_error', 'ilike', 'NI303')],
            [('last_error', 'ilike', 'NI304')],
            [('last_error', 'ilike', 'SF307')],
            [('last_error', 'ilike', 'CF305')],
            [('last_error', 'ilike', 'CV302')],
        ])])

    def _has_recorded_rejection(self):
        self.ensure_one()
        return bool(self.filtered_domain(self._recorded_rejection_domain()))

    @api.model
    def _attention_domain(self, now=None):
        now = now or fields.Datetime.now(); cutoff = now - timedelta(minutes=15)
        return expression.OR([
            self._authority_duplicate_review_domain(),
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
    def _authority_duplicate_review_domain(self):
        # Keep NULL errors in the inverse (healthy) filter as well. A bare
        # NOT LIKE predicate does not match SQL NULL values.
        return [('last_error', '!=', False), ('last_error', '=like', DUPLICATE_REVIEW_PREFIX + '%')]

    @api.model
    def _search_needs_attention(self, operator, value):
        if operator not in ('=', '!=') or value not in (True, False):
            raise ValueError('Use a boolean equality filter for ETA attention.')
        domain = self._attention_domain()
        return domain if bool(value) == (operator == '=') else ['!'] + expression.normalize_domain(domain)

    @api.model
    def _native_partial_progress_is_recent(self, now=None, cron=None):
        """A ready partial native batch deliberately keeps nextcall in the past.

        Require both a recently completed core partial batch and a fresh
        committed invoice-processing marker. A ready date or a live/incomplete
        progress row alone is not evidence that the scheduler is healthy.
        """
        now = now or fields.Datetime.now()
        cron = cron or self.env.ref('tradeline_eta_reliability.cron_eta_queue', raise_if_not_found=False)
        if not cron:
            return False
        cron = cron.sudo()
        if not cron.active or cron.failure_count:
            return False
        self.env['ir.config_parameter'].sudo().flush_model(['key', 'value'])
        self.env.cr.execute('SELECT key,value FROM ir_config_parameter WHERE key IN %s', [(
            'tradeline_eta_reliability.enabled_database',
            'tradeline_eta_reliability.last_processed_at')])
        values = dict(self.env.cr.fetchall())
        if values.get('tradeline_eta_reliability.enabled_database') != self.env.cr.dbname:
            return False
        try:
            processed = fields.Datetime.to_datetime(values.get('tradeline_eta_reliability.last_processed_at'))
        except (ValueError, TypeError):
            return False
        cutoff = now - timedelta(minutes=3)
        if not processed or not cutoff <= processed <= now:
            return False
        return bool(self.env['ir.cron.progress'].sudo().search_count([
            ('cron_id', '=', cron.id), ('done', '>', 0), ('remaining', '>', 0),
            ('timed_out_counter', '=', 0), ('write_date', '>=', cutoff), ('write_date', '<=', now),
        ], limit=1))

    @api.depends('state', 'last_error', 'create_date', 'attempted_at', 'next_attempt',
                 'move_id.e_invoice_date', 'move_id.e_invoice_issue', 'issue_date_policy', 'duplicate_retry_at', 'safe_to_retry',
                 'move_id.amount_total_signed', 'move_id.partner_id.vat', 'move_id.partner_id.company_type')
    def _compute_monitoring(self):
        now = fields.Datetime.now()
        has_mobile_type = 'mobile_type' in self.env['res.partner']._fields
        cron = self.env.ref('tradeline_eta_reliability.cron_eta_queue', raise_if_not_found=False)
        cron = cron.sudo() if cron else cron
        enabled = (bool(cron and cron.active and not cron.failure_count)
                   and self.env['ir.config_parameter'].sudo().get_param(
                       'tradeline_eta_reliability.enabled_database') == self.env.cr.dbname)
        overdue = bool(enabled and cron.nextcall and cron.nextcall < now - timedelta(minutes=3)
                       and not self._native_partial_progress_is_recent(now, cron))
        for job in self:
            anchor = (job.attempted_at or job.move_id.e_invoice_date or job.create_date
                      if job.state in ('accepted', 'unknown') else job.create_date)
            minutes = max(0, int((now - anchor).total_seconds() / 60)) if anchor else 0
            def cairo_time(value, fallback):
                return (fields.Datetime.context_timestamp(job.with_context(tz='Africa/Cairo'), value).strftime('%d %b %Y %H:%M:%S')
                        + ' (Cairo)') if value else fallback
            last_check = cairo_time(job.checked_at, _('not yet completed'))
            next_check = cairo_time(job.next_attempt, _('next worker cycle'))
            raw = job.last_error or ''
            duplicate_review = job._has_authority_duplicate_review()
            date_review = job.state == 'valid' and 'issue date' in (
                raw + ' ' + (job.move_id.e_invoice_issue or '')).lower()
            if date_review and 'issue date' not in raw.lower():
                raw = job.move_id.e_invoice_issue
            detail, codes = error_details(raw)
            lower = detail.lower()
            # ETA requires a valid 14-digit national ID for domestic individual
            # invoices of EGP 25,000 or more (threshold effective 1 Aug 2024).
            # Surface that as the concrete cause of an identification rejection,
            # mirroring the hard block in account_move._get_eta_personal_details.
            partner = job.move_id.partner_id
            individual_id_required = bool(
                partner.company_type == 'person'
                and (not has_mobile_type or partner.mobile_type == 'local')
                and abs(job.move_id.amount_total_signed or 0.0) >= 25000
                and not re.fullmatch(r'[0-9]{14}', (partner.vat or '').strip()))
            actions = []
            if 'CF313' in codes or 'outside the configured eta submission window' in lower:
                actions.append(_('Current ETA issue-date treatment is approved. Correct any remaining reported errors before retrying.')
                               if job.issue_date_policy == 'current' else
                               _('Finance must arrange the approved ETA late-submission route. Keep the original invoice date.'))
            if 'DS301' in codes or 'duplicate' in lower:
                actions.append(_('Reconcile the referenced original ETA receipt before considering another submission.'))
            if (any(code.startswith('NI') for code in codes) or codes.intersection({'SF307', 'CF305'})
                    or any(word in lower for word in ('national id', 'receiver id', 'tax id'))):
                actions.append(
                    _('This domestic individual invoice is EGP 25,000 or more, so ETA requires a valid 14-digit '
                      'national ID. Add the real national ID to the customer record before retrying.')
                    if individual_id_required else
                    _('Verify the customer ID and customer classification in the customer record.'))
            if 'CV302' in codes or any(word in lower for word in ('product', 'itemcode', 'ar_description')):
                actions.append(_('Correct the product details and verify its ETA-approved EGS/GS1 code.'))
            if '4043' in codes or any(word in lower for word in ('signer', 'signing', 'signature')):
                actions.append(_('Check the company signing service and certificate configuration.'))
            if any(word in lower for word in ('serialization', 'calculation review', 'five-decimal', 'tax allocations', 'posted exchange rate')):
                actions.append(_('IT must reconcile ETA calculations with the posted ledger. Preserve the accounting dates and amounts.'))
            if 'eta requires positive quantities' in lower:
                actions.append(_('Finance must review the return/credit-note and replacement-invoice route. Do not invert quantities or remove return lines to force ETA acceptance.'))
            if job.duplicate_retry_at:
                actions.append(_('Wait for the ETA Retry-After deadline; automatic receipt reconciliation will run before any retry.'))
            if duplicate_review:
                reason = detail
                action = _('Finance and IT must compare all currently approved ETA receipts with the original invoice and determine which transactions they represent. Preserve the original documents and accounting values; do not automatically cancel, delete or submit another copy. Refresh uses complete authority history before clearing this warning.')
            elif job.state == 'valid':
                reason = detail if date_review else _('ETA approved this invoice.')
                action = (_('Finance must review the issue-date discrepancy against the existing approved receipt. Do not automatically cancel or resend it.')
                          if date_review else _('No submission action required.'))
            elif job.state == 'review':
                reason = detail or _('Waiting for the worker to verify older ETA receipts before restoring an approval, sending or holding for correction.')
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
                          if detail else _('ETA acknowledged this invoice %(minutes)s minute(s) ago and is processing validation. '
                              'Last confirmed check: %(last)s. Next check: %(next)s. ETA has not provided a completion time.',
                              minutes=minutes, last=last_check, next=next_check))
                action = _('Automatic status checks continue. Investigate prolonged delays; do not submit another copy.')
            elif job.state == 'queued':
                if detail:
                    reason = _('Automatic processing was deferred: %s', detail)
                    action = ' '.join(actions + [
                        _('The worker will retry automatically at the next retry time.') if enabled else
                        _('Restore the ETA scheduled worker before automatic retries can continue.')])
                elif not enabled:
                    reason = _('The automatic ETA worker is disabled, failing or not enabled for this database.')
                    action = _('Restore the ETA scheduled worker and its production database activation. The invoice remains queued.')
                elif overdue:
                    reason = _('The automatic ETA scheduled worker is overdue.')
                    action = _('Check scheduler availability and database delays. The invoice remains queued.')
                else:
                    reason = (_('Waiting in the automatic submission queue; %s earlier attempt(s) remain recorded.', job.attempts)
                              if job.attempts else _('Waiting in the automatic submission queue; no submission attempt has been recorded yet.'))
                    action = _('The enabled worker will process this invoice. Check queue progress if the delay continues.')
            else:
                reason = detail or (_('ETA marked this invoice invalid; detailed validation information needs review.')
                                    if job.state == 'invalid' else _('Processing is held for correction or review.'))
                action = ' '.join(actions) or _('Review the detailed error and correct the source issue before an accounting manager queues a verified retry.')
            base_reason = reason
            if job.state == 'unknown' and not (job.last_error or '').startswith('ETA receipt search completed '):
                reason = _('ETA receipt verification is pending for %(minutes)s minute(s) since the attempt. '
                    'Last check: %(last)s. Next check: %(next)s.', minutes=minutes, last=last_check, next=next_check)
                action = _('Check ETA status to request a receipt lookup now. Automatic verification continues; '
                    'if no receipt can be established, Accounting or IT must review delivery before any resend.')
            if job.state in ('blocked', 'invalid', 'review', 'queued') and actions:
                if any(word in lower for word in ('national id', 'receiver id', 'customer id', 'tax id')) or any(code.startswith('NI') for code in codes) or codes.intersection({'SF307', 'CF305'}):
                    reason = (_('This individual invoice is EGP 25,000 or more; ETA requires a valid 14-digit national ID, which is missing from the customer record.')
                              if individual_id_required else
                              _('Customer identification needs attention. Verify the customer type and the real national or tax ID.'))
                elif 'CV302' in codes:
                    reason = _('An invoice product code needs ETA approval or correction.')
                elif 'CF313' in codes or 'outside the configured eta submission window' in lower:
                    reason = _('The invoice is outside the ETA submission date window. Review the permitted date treatment.')
            if any(word in lower for word in ('communication failed', 'connection', 'timed out', 'timeout', 'unreadable response')) and job.state in ('queued', 'accepted', 'review'):
                reason = (_('ETA communication could not be completed. The invoice details may be correct; the latest ETA result is unconfirmed.'))
                action = _('Check ETA status now. Automatic recovery continues. A connection failure alone does not establish that ETA is down.')
            elif 'http 429' in lower:
                reason = _('ETA asked us to slow down. This invoice is waiting for the required retry time.')
            elif any(word in lower for word in ('signer is unavailable', 'signing service is unavailable')):
                reason = _('The company signing service is unavailable. IT needs to restore signing; invoice data may be correct.')
            elif 'authentication' in lower or 'http 401' in lower or 'http 403' in lower:
                reason = _('ETA login or access failed. Ask your accounting administrator to check the company connection.')
            job.reason_summary = base_reason
            job.staff_summary = reason
            job.required_action = action
            job.waiting_minutes = minutes if job.state != 'valid' else 0
            job.needs_attention = (job.state in ('blocked', 'invalid') or date_review or duplicate_review
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

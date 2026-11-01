"""Show proven rejections as corrections while preserving transport/history holds."""
import re

from odoo import api, models, _
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import SQL

from .eta_job import DUPLICATE_REVIEW_PREFIX
from .eta_monitoring import error_details


def review_dependencies(model):
    depends = ['state', 'payload', 'payload_hash', 'attempts', 'attempted_at',
        'move_id', 'move_id.name', 'company_id', 'safe_to_retry', 'duplicate_retry_at',
        'duplicate_payload_hash', 'delivery_review_proof', 'last_error', 'checked_at',
        'submission_uuid', 'historical_review', 'move_id.e_invoice_status',
        'move_id.e_invoice_issue', 'move_id.e_invoice_uuid', 'move_id.l10n_eg_uuid',
        'move_id.e_invoice_date', 'move_id.e_invoice_sent', 'move_id.partner_id.vat',
        'move_id.partner_id.company_type', 'move_id.invoice_line_ids.product_id',
        'move_id.amount_total', 'move_id.amount_total_signed', 'move_id.currency_id',
        'create_date', 'next_attempt', 'issue_date_policy']
    for name in ('e_invoicing_code', 'code_type'):
        if name in model.env['product.product']._fields:
            depends.append('move_id.invoice_line_ids.product_id.' + name)
    return depends


class EtaReviewRejectionPresentation(models.Model):
    _inherit = 'tradeline.eta.job'

    @api.model
    def _review_confirmed_rejection_sql(self):
        """Metadata only. A newer send cannot borrow an older Invalid receipt."""
        return SQL("""(
            label_job.state='review'
            AND label_job.duplicate_retry_at IS NULL
            AND COALESCE(label_job.duplicate_payload_hash,'')=''
            AND COALESCE(label_job.last_error,'') NOT LIKE %s
            AND NOT (COALESCE(label_move.e_invoice_uuid,'')<>''
                AND COALESCE(label_move.l10n_eg_uuid,'')<>''
                AND label_move.e_invoice_uuid<>label_move.l10n_eg_uuid)
            AND (
                (label_move.e_invoice_status='Invalid'
                    AND COALESCE(NULLIF(label_move.e_invoice_uuid,''),NULLIF(label_move.l10n_eg_uuid,'')) IS NOT NULL
                    AND (
                        (COALESCE(label_job.attempts,0)=0 AND label_job.attempted_at IS NULL
                            AND COALESCE(label_job.payload_hash,'')='' AND COALESCE(label_job.payload,'')='')
                        OR (label_job.attempts>0 AND label_job.attempted_at IS NOT NULL
                            AND COALESCE(label_job.payload_hash,'')<>'' AND COALESCE(label_job.payload,'')<>''
                            AND COALESCE(label_move.e_invoice_uuid,'')<>''
                            AND COALESCE(label_job.submission_uuid,'')<>''
                            AND label_job.checked_at>=label_job.attempted_at
                            AND label_move.e_invoice_date>=label_job.attempted_at
                            AND label_move.e_invoice_date<=label_job.checked_at
                            AND label_job.last_error LIKE %s
                            AND label_move.e_invoice_issue=label_job.last_error)))
                OR (label_move.e_invoice_status='Error' AND label_job.safe_to_retry IS TRUE
                    AND label_job.attempts>0 AND label_job.attempted_at IS NOT NULL
                    AND COALESCE(label_job.payload_hash,'')<>'' AND COALESCE(label_job.payload,'')<>''
                    AND COALESCE(label_move.e_invoice_uuid,'')='' AND COALESCE(label_move.l10n_eg_uuid,'')=''
                    AND COALESCE(label_job.submission_uuid,'')=''
                    AND label_job.last_error LIKE %s AND label_move.e_invoice_issue=label_job.last_error)))""",
            DUPLICATE_REVIEW_PREFIX + '%', '{"status": "Invalid"%', 'ETA rejected the submission (%')

    def _review_rejection_labels(self):
        if not self:
            return {}
        self.flush_model(['state', 'safe_to_retry', 'attempts', 'attempted_at', 'payload_hash', 'payload',
                          'move_id', 'last_error', 'checked_at', 'submission_uuid', 'duplicate_retry_at', 'duplicate_payload_hash'])
        self.env['account.move'].flush_model(['e_invoice_status', 'e_invoice_issue', 'e_invoice_uuid',
                                             'l10n_eg_uuid', 'e_invoice_date'])
        self.env.cr.execute(SQL("""SELECT label_job.id, CASE WHEN %s THEN 'invalid' ELSE 'review' END
            FROM tradeline_eta_job label_job JOIN account_move label_move ON label_move.id=label_job.move_id
            WHERE label_job.id IN %s AND label_job.state='review'""",
            self._review_confirmed_rejection_sql(), tuple(self.ids)))
        return dict(self.env.cr.fetchall())

    def _review_local_source_hold(self):
        """Verify a recorded local preparation error; never call it ETA rejection."""
        self.ensure_one()
        job = self.sudo().with_context(prefetch_fields=False)
        move = job.move_id
        raw = job.last_error or ''
        product = re.match(r'Missing One of required details \[(e_invoicing_code|code_type)\] for product \[', raw)
        def customer_error(message):
            return (message.startswith('Missing Tax ID for partner [')
                or message.startswith('Missing National ID for partner [')
                or message.startswith('National ID for partner [') and 'is not valid' in message
                or message.startswith('National ID is required for domestic individual invoices')
                or message.startswith('A 14-digit national ID is required for domestic individual invoices'))
        customer = customer_error(raw)
        if (not (product or customer) or job.state != 'review' or job.attempts or job.attempted_at
                or job.payload_hash or job.submission_uuid or job.duplicate_retry_at or job.duplicate_payload_hash
                or move.e_invoice_uuid or move.l10n_eg_uuid or move.e_invoice_sent
                or move.e_invoice_status in ('Valid','Invalid','Submitted','Rejected','Cancelled')):
            return False
        # Return only presence, never load every retained signed body during a
        # presentation backfill. The actual transport markers remain untouched.
        self.env.cr.execute('SELECT COALESCE(payload,\'\')<>\'\' FROM tradeline_eta_job WHERE id=%s', [job.id])
        row = self.env.cr.fetchone()
        if not row or row[0]:
            return False
        if product:
            products = move.invoice_line_ids.mapped('product_id')
            return bool(products and any(not getattr(item, product.group(1), False) for item in products))
        try:
            move._get_eta_personal_details(move.partner_id, move.amount_total)
        except (UserError, ValidationError) as exc:
            return customer_error(str(exc))
        except (AttributeError, ValueError):
            return False
        return False

    @api.depends(review_dependencies)
    def _compute_staff_display_state(self):
        super()._compute_staff_display_state()
        reviews = self.filtered(lambda job: job.state == 'review')
        labels = reviews._review_rejection_labels()
        for job in reviews:
            job.staff_state = ('invalid' if labels.get(job.id) == 'invalid' else
                               'blocked' if job._review_local_source_hold() else 'review')

    @api.depends(lambda model: review_dependencies(model) + ['staff_state'])
    def _compute_monitoring(self):
        super()._compute_monitoring()
        for job in self.filtered(lambda record: record.state == 'review'):
            if job.staff_state == 'invalid':
                _detail, codes = error_details(job.last_error or job.move_id.e_invoice_issue)
                reasons, corrections = [], []
                if 'CV302' in codes:
                    reasons.append(_('A product code needs ETA approval or correction.'))
                    corrections.append(_('Correct the product details and verify the real ETA-approved EGS/GS1 code.'))
                if 'CF320' in codes:
                    reasons.append(_('The customer country or classification does not match the ETA document.'))
                    corrections.append(_('Verify the customer’s real type and country, and correct the mismatching customer details.'))
                if any(code.startswith('NI') for code in codes) or codes.intersection({'CF305','SF307'}):
                    reasons.append(_('Customer identification is missing or invalid.'))
                    corrections.append(_('Verify the real customer type and required identification, then correct the reported ID issue.'))
                if not corrections:
                    reasons.append(_('The recorded invoice data needs correction.'))
                    corrections.append(_('Review and correct the reported invoice, customer or product issue.'))
                job.staff_summary = _('ETA rejected an earlier submission. %s', ' '.join(reasons))
                job.required_action = ' '.join(corrections) + ' ' + _(
                    'Then use Sync invoice. It checks existing ETA receipts before sending a corrected version when safe.')
            elif job.staff_state == 'blocked':
                job.staff_summary = _('Invoice data needs correction before ETA processing can continue. This is a local data check, not a confirmed ETA rejection.')
                job.required_action = _('Correct the missing product code or customer identification shown in the recorded issue. '
                    'Then use Sync invoice to recheck the data and existing ETA receipts before sending when safe.')

    @api.model
    def _refresh_review_staff_labels(self):
        """Bounded presentation-only upgrade; no queue, attempts or invoice writes."""
        if not self.env.su:
            raise AccessError(_('Administrative presentation refresh required.'))
        self.flush_model(['state','staff_state'])
        last_id = scanned = changed = 0
        while True:
            self.env.cr.execute("""SELECT id FROM tradeline_eta_job WHERE state='review' AND id>%s
                ORDER BY id LIMIT 250 FOR UPDATE SKIP LOCKED""", [last_id])
            ids = [row[0] for row in self.env.cr.fetchall()]
            if not ids:
                break
            jobs = self.with_context(prefetch_fields=False).browse(ids)
            labels = jobs._review_rejection_labels()
            jobs.fetch(['state','staff_state','attempts','attempted_at','payload_hash','submission_uuid',
                        'duplicate_retry_at','duplicate_payload_hash','move_id','last_error'])
            jobs.mapped('move_id').fetch(['e_invoice_uuid','l10n_eg_uuid','e_invoice_sent','e_invoice_status'])
            for job in jobs:
                label = labels.get(job.id, 'review')
                if label == 'review' and job._review_local_source_hold():
                    label = 'blocked'
                self.env.cr.execute("""UPDATE tradeline_eta_job SET staff_state=%s
                    WHERE id=%s AND state='review' AND staff_state IS DISTINCT FROM %s""", [label,job.id,label])
                changed += self.env.cr.rowcount
            jobs.invalidate_recordset(['staff_state'])
            scanned += len(ids); last_id = ids[-1]
        return {'scanned':scanned,'changed':changed}

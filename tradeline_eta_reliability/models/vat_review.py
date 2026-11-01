"""VAT submission policy and receipt-backed staff review; no automatic corrections."""
import json
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation

import pytz

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError
from odoo.tools import SQL

from .submission_safety import taxpayer


def document_vat(document):
    """Return the actual T1 total in EGP; other taxes do not satisfy VAT."""
    items = document.get('taxTotals')
    if not isinstance(items, list):
        return Decimal(0)
    try:
        amount = sum((Decimal(str(item.get('amount', 0))) for item in items
                      if isinstance(item, dict) and item.get('taxType') == 'T1'), Decimal(0))
        return amount if amount.is_finite() else Decimal(0)
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)


class EtaVatReviewMove(models.Model):
    _inherit = 'account.move'

    eta_vat_amount = fields.Monetary(string='Current invoice VAT', compute='_compute_eta_vat_review', compute_sudo=True)
    eta_vat_missing = fields.Boolean(string='Missing VAT', compute='_compute_eta_vat_review',
        compute_sudo=True, search='_search_eta_vat_missing')
    # Ordinary stored audit facts avoid recomputing the complete historical
    # accounting ledger during upgrade. They never authorize transmission.
    eta_receipt_vat_uuid = fields.Char(readonly=True, copy=False, index=True)
    eta_receipt_vat_checked_at = fields.Datetime(string='Saved receipt reviewed', readonly=True, copy=False)
    eta_receipt_vat_missing = fields.Boolean(string='Receipt has no VAT', readonly=True, copy=False, index=True)
    eta_receipt_vat_egp = fields.Float(string='Original receipt VAT (EGP)', readonly=True, copy=False, digits=(16, 5))
    eta_vat_review_state = fields.Selection([
        ('missing', 'VAT missing — sending blocked'),
        ('approved_missing', 'Approved without VAT — correct it'),
        ('receipt_review', 'Receipt unavailable — reconcile ETA'),
        ('source_changed', 'Current VAT differs from saved receipt'),
        ('correcting', 'VAT correction in progress'),
        ('corrected', 'VAT correction completed'),
        ('ready', 'VAT present'), ('other', 'Credit / other document')],
        string='VAT review', compute='_compute_eta_vat_review', compute_sudo=True)
    eta_vat_review_note = fields.Text(string='VAT next step', compute='_compute_eta_vat_review', compute_sudo=True)

    @api.model
    def _eta_vat_taxes(self):
        return self.env['account.tax'].sudo().with_context(active_test=False).search([
            '|', ('type_code_id.code', '=', 'T1'), ('l10n_eg_eta_code', '=ilike', r't1\_%')])

    @api.depends('move_type', 'state', 'line_ids.amount_currency', 'line_ids.tax_line_id',
                 'line_ids.tax_line_id.type_code_id.code', 'line_ids.tax_line_id.l10n_eg_eta_code',
                 'e_invoice_uuid', 'l10n_eg_uuid', 'e_invoice_sent', 'e_invoice_status', 'l10n_eg_eta_json_doc_id',
                 'eta_job_ids.state', 'eta_job_ids.attempts', 'eta_job_ids.attempted_at', 'eta_job_ids.safe_to_retry',
                 'eta_receipt_vat_uuid', 'eta_receipt_vat_missing', 'eta_receipt_vat_checked_at',
                 'eta_vat_correction_ids.state', 'eta_vat_correction_ids.e_invoice_status',
                 'eta_vat_correction_ids.e_invoice_uuid', 'eta_vat_correction_ids.eta_receipt_vat_checked_at',
                 'eta_vat_correction_ids.eta_receipt_vat_missing', 'eta_vat_correction_ids.eta_receipt_vat_uuid')
    def _compute_eta_vat_review(self):
        taxes = self._eta_vat_taxes()
        for move in self:
            amount = abs(sum(move.line_ids.filtered(lambda line: line.tax_line_id in taxes).mapped('amount_currency')))
            missing = move.move_type == 'out_invoice' and move.currency_id.is_zero(amount)
            uuid = move.e_invoice_uuid or move.l10n_eg_uuid
            conflicting = bool(move.e_invoice_uuid and move.l10n_eg_uuid and move.e_invoice_uuid != move.l10n_eg_uuid)
            audited = bool(uuid and not conflicting and move.eta_receipt_vat_uuid == uuid and move.eta_receipt_vat_checked_at)
            receipt_missing = move.eta_receipt_vat_missing
            job = move.eta_job_ids
            sent = bool(uuid or move.e_invoice_sent or job.state in ('unknown', 'accepted')
                or job and (job.attempts or job.attempted_at) and not job.safe_to_retry)
            if missing and sent and not audited:
                # Resolve a visible historical tax issue from its saved signed
                # document immediately. Never ask staff to interpret VAT or
                # make a government request while rendering an invoice list.
                saved = move._eta_saved_vat_document()
                if saved:
                    audited = True
                    receipt_missing = document_vat(saved[1]) <= 0
            docs = move.eta_vat_correction_ids.filtered(lambda record: record.state != 'cancel')
            credits = docs.filtered(lambda record: record.move_type == 'out_refund')
            replacements = docs.filtered(lambda record: record.move_type == 'out_invoice')
            completed = bool(credits and replacements and all(record.state == 'posted'
                and record.e_invoice_uuid and record.e_invoice_status == 'Valid' for record in credits | replacements)
                and all(record.eta_receipt_vat_checked_at and not record.eta_receipt_vat_missing
                    and record.eta_receipt_vat_uuid == record.e_invoice_uuid for record in replacements))
            state, note = 'ready', _('VAT is present in the current invoice. ETA receipt and validation checks still apply.')
            if move.move_type != 'out_invoice':
                state, note = 'other', _('Credit notes preserve the taxes of the original receipt they reverse.')
            elif audited and receipt_missing:
                if completed:
                    state, note = 'corrected', _('The linked credit and VAT replacement are approved by ETA. The original receipt is preserved.')
                elif docs:
                    state, note = 'correcting', _('Open VAT Corrections. Review/post the credit first; send the VAT replacement after ETA approves the credit.')
                else:
                    state = 'approved_missing' if move.e_invoice_status == 'Valid' else 'receipt_review'
                    note = _('The saved ETA receipt contains no positive VAT. Use Correct VAT to prepare linked correction drafts; keep the original receipt.')
            elif missing and sent:
                state = 'source_changed' if audited else 'receipt_review'
                note = (_('The saved receipt includes VAT but the current accounting record does not. Finance must reconcile the difference before any correction.')
                    if audited else _('No matching saved ETA receipt is available. Check ETA status to retrieve the signed document. An unresolved send must be reconciled before a correction or resend.'))
            elif missing:
                state, note = 'missing', _('Sending is blocked until positive VAT is recorded. Use Correct VAT, review the draft, and post it again.')
            move.eta_vat_amount = amount
            move.eta_vat_missing = missing
            move.eta_vat_review_state = state
            move.eta_vat_review_note = note

    @api.model
    def _search_eta_vat_missing(self, operator, value):
        if operator not in ('=', '!=') or value not in (True, False):
            raise ValueError('Use boolean equality for missing VAT.')
        self.env['account.move.line'].flush_model(['move_id', 'tax_line_id', 'amount_currency'])
        query = self._search([('move_type', '=', 'out_invoice')])
        query.add_where(SQL("""NOT EXISTS (
            SELECT 1 FROM account_move_line v
            WHERE v.move_id=account_move.id AND v.tax_line_id IN %s
            GROUP BY v.move_id HAVING ABS(SUM(v.amount_currency)) >=
                (SELECT rounding / 2 FROM res_currency WHERE id=account_move.currency_id))""",
            tuple(self._eta_vat_taxes().ids) or (0,)))
        domain = [('id', 'in', query)]
        return domain if bool(value) == (operator == '=') else ['!'] + domain

    def write(self, vals):
        if not self.env.su and set(vals).intersection({
                'eta_receipt_vat_uuid', 'eta_receipt_vat_checked_at', 'eta_receipt_vat_missing', 'eta_receipt_vat_egp'}):
            raise AccessError(_('Saved ETA VAT findings can only be recorded by receipt verification.'))
        return super().write(vals)

    def _eta_record_receipt_vat(self, uuid, document):
        self.ensure_one()
        if (not uuid or uuid not in (self.e_invoice_uuid, self.l10n_eg_uuid)
                or self.e_invoice_uuid and self.l10n_eg_uuid and self.e_invoice_uuid != self.l10n_eg_uuid
                or not isinstance(document, dict) or document.get('internalID') != self.name
                or taxpayer(document.get('issuer', {}).get('id')) != taxpayer(self.company_id.vat)):
            return False
        amount = document_vat(document)
        self.sudo().write({'eta_receipt_vat_uuid': uuid, 'eta_receipt_vat_checked_at': fields.Datetime.now(),
            'eta_receipt_vat_missing': amount <= 0, 'eta_receipt_vat_egp': float(amount)})
        return True

    def _eta_review_saved_vat(self):
        """Read the immutable matching native archive; no HTTP or queue changes."""
        checked = 0
        for move in self:
            saved = move._eta_saved_vat_document()
            if saved:
                checked += bool(move._eta_record_receipt_vat(*saved))
        return checked

    def _eta_saved_vat_document(self):
        """Read only an identity-matched archive; no writes or network access."""
        self.ensure_one()
        attachment = self.sudo().l10n_eg_eta_json_doc_id
        if (self.e_invoice_uuid and self.l10n_eg_uuid and self.e_invoice_uuid != self.l10n_eg_uuid
                or not attachment or attachment.res_model != 'account.move'
                or attachment.res_id != self.id or attachment.company_id and attachment.company_id != self.company_id):
            return False
        try:
            saved = json.loads(attachment.raw)
            uuid = saved.get('response', {}).get('l10n_eg_uuid')
            document = saved.get('request')
            if isinstance(document, str):
                document = json.loads(document)
            if (uuid and uuid in (self.e_invoice_uuid, self.l10n_eg_uuid)
                    and isinstance(document, dict) and document.get('internalID') == self.name
                    and taxpayer(document.get('issuer', {}).get('id')) == taxpayer(self.company_id.vat)):
                return uuid, document
        except (ValueError, TypeError, AttributeError):
            pass
        finally:
            attachment.invalidate_recordset(['raw', 'datas', 'db_datas'])
        return False

    @api.model
    def action_eta_today(self):
        if not (self.env.su or self.env.user.has_group('account.group_account_readonly')
                or self.env.user.has_group('account.group_account_invoice')):
            raise AccessError(_('Accounting access is required to view ETA invoices.'))
        self.check_access('read')
        day = fields.Date.context_today(self.with_context(tz='Africa/Cairo'))
        zone = pytz.timezone('Africa/Cairo')
        start = zone.localize(datetime.combine(day, time.min), is_dst=False).astimezone(pytz.utc).replace(tzinfo=None)
        end = zone.localize(datetime.combine(day + timedelta(days=1), time.min), is_dst=False).astimezone(pytz.utc).replace(tzinfo=None)
        start, end = fields.Datetime.to_string(start), fields.Datetime.to_string(end)
        domain = [('move_type', 'in', ['out_invoice', 'out_refund']), '|', '|', '|',
            ('invoice_date', '=', fields.Date.to_string(day)),
            '&', ('create_date', '>=', start), ('create_date', '<', end),
            '&', ('e_invoice_date', '>=', start), ('e_invoice_date', '<', end),
            '&', ('eta_job_ids.attempted_at', '>=', start), ('eta_job_ids.attempted_at', '<', end)]
        action = self.env['ir.actions.actions']._for_xml_id('tradeline_eta_reliability.action_eta_all_invoices')
        action.update(name=_('ETA — Today (%s)', day), domain=domain,
            context={'create': False, 'group_by': ['state', 'e_invoice_status']})
        return action

    def action_eta_review_saved_vat(self):
        self.check_access('read')
        if not (self.env.su or self.env.user.has_group('account.group_account_readonly')
                or self.env.user.has_group('account.group_account_invoice')):
            raise AccessError(_('Accounting access is required to review saved ETA receipts.'))
        count = self._eta_review_saved_vat()
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'title': _('Saved VAT receipts reviewed'), 'message': _('%s matching saved receipts reviewed. Use Check ETA status for missing receipts.', count),
            'type': 'info', 'next': {'type': 'ir.actions.client', 'tag': 'reload'}}}

    @api.model
    def _cron_eta_review_recent_vat(self):
        day = fields.Date.context_today(self.with_context(tz='Africa/Cairo'))
        start = pytz.timezone('Africa/Cairo').localize(datetime.combine(day, time.min), is_dst=False)
        start = start.astimezone(pytz.utc).replace(tzinfo=None)
        pending = [('move_type', '=', 'out_invoice'), ('state', '=', 'posted'),
            ('l10n_eg_eta_json_doc_id', '!=', False), ('eta_receipt_vat_checked_at', '=', False)]
        moves = self.sudo().search(pending + [('eta_vat_missing', '=', True)], limit=100)
        if len(moves) < 100:
            moves |= self.sudo().search(pending + [('id', 'not in', moves.ids),
                ('e_invoice_status', '=', 'Valid'), ('e_invoice_date', '>=', start)], limit=100-len(moves))
        moves._eta_review_saved_vat()


class EtaVatSubmissionGate(models.Model):
    _inherit = 'tradeline.eta.job'

    vat_review_state = fields.Selection(related='move_id.eta_vat_review_state', readonly=True)

    def _preflight_submission(self, client):
        # This outermost guard covers cron, manual bypass, prepared retries and
        # native EDI delegation before the durable marker or government POST.
        # Reconcile late approvals and duplicate receipts first. A read-only
        # recovery must remain possible even when an old receipt has no VAT.
        allowed = super()._preflight_submission(client)
        if allowed:
            self._assert_positive_vat_submission()
        return allowed

    def _assert_positive_vat_submission(self):
        move = self.move_id
        if move.move_type == 'out_invoice':
            if move.eta_vat_missing:
                raise UserError(_('VAT is missing. This customer invoice cannot be sent to ETA. Use Correct VAT, review the draft, and post it again.'))
            document = json.loads(self.payload)['documents'][0]
            if document_vat(document) <= 0:
                raise UserError(_('The prepared ETA document has no positive VAT. Regenerate it from the corrected invoice before sending.'))
        return True

    def _apply_remote(self, data):
        result = super()._apply_remote(data)
        document = data.get('document')
        if isinstance(document, str):
            try:
                document = json.loads(document)
            except ValueError:
                document = None
        if data.get('status') == 'Valid' and isinstance(document, dict):
            self.move_id._eta_record_receipt_vat(data.get('uuid'), document)
        return result

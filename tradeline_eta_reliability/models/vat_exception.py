"""Explicit zero-rate classifications and invoice-scoped VAT overrides."""
import hashlib
import json

import psycopg2

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError
from odoo.tools import SQL

from .vat_review import document_vat

# ETA's published T1 zero-rate/exemption classifications, not general sales V009.
EXEMPT_SUBTYPES = frozenset('V%03d' % number for number in range(1, 9))
APPROVAL_FIELDS = frozenset({'eta_vat_bypass_reason', 'eta_vat_bypass_by',
    'eta_vat_bypass_at', 'eta_vat_bypass_hash'})
APPROVAL_WRITE = object()


def exemption_subtype(tax):
    legacy = (tax.type_code_id.code or '').upper()
    subtype = (tax.sub_type_code_id.code or '').upper()
    native = (tax.l10n_eg_eta_code or '').upper()
    if native:
        parts = native.split('_')
        if len(parts) != 2 or legacy and legacy != parts[0] or subtype and subtype != parts[1]:
            return False
        legacy, subtype = parts
    return subtype if (legacy == 'T1' and subtype in EXEMPT_SUBTYPES
        and tax.amount_type == 'percent' and tax.amount == 0 and not tax.is_deduction) else False


class EtaVatExceptionMove(models.Model):
    _inherit = 'account.move'

    eta_vat_bypass_reason = fields.Text(string='VAT bypass reason', readonly=True, copy=False)
    eta_vat_bypass_by = fields.Many2one('res.users', string='VAT bypass approved by', readonly=True, copy=False)
    eta_vat_bypass_at = fields.Datetime(string='VAT bypass approved on', readonly=True, copy=False)
    eta_vat_bypass_hash = fields.Char(readonly=True, copy=False, groups='base.group_system')

    @api.model_create_multi
    def create(self, vals_list):
        if any(APPROVAL_FIELDS.intersection(vals) for vals in vals_list):
            raise AccessError(_('Use Send without VAT to record a VAT bypass.'))
        return super().create(vals_list)

    def write(self, vals):
        if APPROVAL_FIELDS.intersection(vals) and self.env.context.get('_eta_vat_approval') is not APPROVAL_WRITE:
            raise AccessError(_('Use Send without VAT to record a VAT bypass.'))
        return super().write(vals)

    @api.model
    def _eta_exemption_taxes(self):
        candidates = self._eta_vat_taxes().filtered(lambda tax: tax.amount == 0)
        return candidates.filtered(exemption_subtype)

    def _eta_exemption_codes(self):
        self.ensure_one()
        lines = self.invoice_line_ids.filtered(lambda line: line.display_type == 'product')
        codes = []
        for line in lines:
            vat = line.tax_ids & self._eta_vat_taxes()
            if len(vat) != 1 or not exemption_subtype(vat):
                return False
            codes.append(exemption_subtype(vat))
        return codes or False

    @api.model
    def _eta_exemption_sql(self):
        """Same all-product-lines rule for bounded, permission-scoped searches."""
        self.env['account.move.line'].flush_model(['move_id', 'display_type', 'tax_ids'])
        exempt = tuple(self._eta_exemption_taxes().ids) or (0,)
        vat = tuple(self._eta_vat_taxes().ids) or (0,)
        return SQL("""(EXISTS (SELECT 1 FROM account_move_line ex_line
            WHERE ex_line.move_id=account_move.id AND ex_line.display_type='product')
            AND NOT EXISTS (SELECT 1 FROM account_move_line ex_line
            WHERE ex_line.move_id=account_move.id AND ex_line.display_type='product' AND (
                NOT EXISTS (SELECT 1 FROM account_move_line_account_tax_rel ex_rel
                    WHERE ex_rel.account_move_line_id=ex_line.id AND ex_rel.account_tax_id IN %s)
                OR EXISTS (SELECT 1 FROM account_move_line_account_tax_rel ex_rel
                    WHERE ex_rel.account_move_line_id=ex_line.id AND ex_rel.account_tax_id IN %s
                    AND ex_rel.account_tax_id <> (SELECT ex_one.account_tax_id
                        FROM account_move_line_account_tax_rel ex_one
                        WHERE ex_one.account_move_line_id=ex_line.id AND ex_one.account_tax_id IN %s LIMIT 1)))))""", exempt, vat, exempt)

    def _eta_document_exemption_matches(self, document):
        codes = self._eta_exemption_codes()
        lines = document.get('invoiceLines', [])
        if not codes or not isinstance(lines, list) or len(lines) != len(codes) or document_vat(document) != 0:
            return False
        for line, subtype in zip(lines, codes):
            items = [item for item in line.get('taxableItems', []) if item.get('taxType') == 'T1']
            if (len(items) != 1 or items[0].get('subType') != subtype
                    or items[0].get('amount') != 0 or items[0].get('rate') != 0):
                return False
        return True

    def _eta_vat_bypass_fingerprint(self):
        self.ensure_one()
        # Bind approval to this invoice's actual source, not a forgeable RPC flag.
        # Rendering does not sign a document, contact ETA, or change the queue.
        header = self.sudo().read(['name', 'move_type', 'state', 'company_id', 'partner_id',
            'journal_id', 'currency_id', 'invoice_date', 'invoice_currency_rate',
            'amount_untaxed', 'amount_tax', 'amount_total'])[0]
        lines = self.invoice_line_ids.sorted('id').sudo().read(['display_type', 'product_id',
            'name', 'account_id', 'quantity', 'price_unit', 'discount', 'tax_ids'])
        taxes = self.invoice_line_ids.tax_ids.sorted('id').sudo().read(['amount', 'amount_type',
            'type_code_id', 'sub_type_code_id', 'l10n_eg_eta_code', 'price_include', 'is_deduction'])
        value = {'database': self.env.cr.dbname, 'header': header, 'lines': lines, 'taxes': taxes}
        return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()

    def _eta_vat_bypass_current(self):
        self.ensure_one()
        return bool(self.eta_vat_bypass_reason and self.eta_vat_bypass_by and self.eta_vat_bypass_at
            and self.sudo().eta_vat_bypass_hash == self._eta_vat_bypass_fingerprint())

    @api.model
    def _eta_vat_workflow_sql(self):
        route = super()._eta_vat_workflow_sql()
        # Approved zero-VAT receipts with an explicit exemption must not be
        # advertised as needing 14% VAT. Preserve conflict/correction routes.
        return SQL("""CASE WHEN account_move.move_type <> 'out_invoice' THEN NULL
            WHEN %s AND COALESCE(account_move.e_invoice_uuid,'')=''
            AND COALESCE(account_move.l10n_eg_uuid,'')='' AND account_move.e_invoice_sent IS NOT TRUE
            AND NOT EXISTS (SELECT 1 FROM tradeline_eta_job ex_job WHERE ex_job.move_id=account_move.id
                AND (ex_job.state IN ('unknown','accepted','review') OR ex_job.historical_review IS TRUE
                    OR ex_job.attempted_at IS NOT NULL OR COALESCE(ex_job.attempts,0)>0))
            THEN NULL WHEN %s AND (%s)='approved' THEN 'exempt'
            ELSE %s END""", self._eta_exemption_sql(), self._eta_exemption_sql(), route, route)

    @api.depends('invoice_line_ids.tax_ids', 'invoice_line_ids.display_type',
        'invoice_line_ids.tax_ids.amount', 'invoice_line_ids.tax_ids.amount_type',
        'invoice_line_ids.tax_ids.is_deduction', 'invoice_line_ids.tax_ids.type_code_id.code',
        'invoice_line_ids.tax_ids.sub_type_code_id.code', 'invoice_line_ids.tax_ids.l10n_eg_eta_code')
    def _compute_eta_vat_workflow(self):
        return super()._compute_eta_vat_workflow()

    def action_post_sign_invoices(self):
        """Old native Sign buttons must enter Ahmed's queue, never a thumb drive."""
        self.env['res.users']._eta_check_workspace_access()
        for move in self:
            move._eta_staff_access()
        if len(self) == 1:
            return self.action_eta_send_now()
        return self.action_send_electronic_invoice()

    def action_eta_send_without_vat(self):
        self.ensure_one()
        self.env['res.users']._eta_check_workspace_access()
        self._eta_staff_access()
        if self.move_type != 'out_invoice':
            raise UserError(_('VAT bypass is for customer invoices. Credit notes retain their original tax treatment.'))
        if self.e_invoice_status in ('Valid', 'Submitted'):
            raise UserError(_('This invoice already has an ETA receipt. Check its status instead of sending again.'))
        return {'type': 'ir.actions.act_window', 'name': _('Send without VAT'),
            'res_model': 'tradeline.eta.vat.bypass', 'view_mode': 'form', 'target': 'new',
            'context': {'default_move_id': self.id}}

    def _eta_approve_vat_bypass(self, reason):
        self.ensure_one()
        self.env['res.users']._eta_check_workspace_access()
        self._eta_staff_access()
        reason = (reason or '').strip()
        if not reason:
            raise UserError(_('Enter the reason for sending this invoice without VAT.'))
        if self.e_invoice_status in ('Valid', 'Submitted'):
            raise UserError(_('This invoice already has an ETA receipt. Check its status instead.'))
        try:
            with self.env.cr.savepoint():
                self.env.cr.execute('SELECT id FROM account_move WHERE id=%s FOR UPDATE NOWAIT', [self.id])
        except psycopg2.errors.LockNotAvailable as exc:
            raise UserError(_('This invoice is being processed. Refresh ETA status before approving its VAT bypass.')) from exc
        self.invalidate_recordset()
        self._eta_staff_access()
        if self.move_type != 'out_invoice' or self.e_invoice_status in ('Valid', 'Submitted'):
            raise UserError(_('Refresh this invoice before approving a VAT bypass; it may already have an ETA receipt.'))
        values = {'eta_vat_bypass_reason': reason, 'eta_vat_bypass_by': self.env.uid,
            'eta_vat_bypass_at': fields.Datetime.now(), 'eta_vat_bypass_hash': self._eta_vat_bypass_fingerprint()}
        self.with_context(_eta_vat_approval=APPROVAL_WRITE).sudo().write(values)
        # Private audit: financial exceptions must not enter shared follower mail.
        self.env['ir.attachment'].sudo().create({'name': 'ETA_VAT_bypass_approval.json',
            'res_model': 'account.move', 'res_id': self.id, 'company_id': self.company_id.id,
            'public': False, 'mimetype': 'application/json', 'raw': json.dumps(values, default=str).encode()})


class EtaVatExceptionJob(models.Model):
    _inherit = 'tradeline.eta.job'

    @api.depends('state', 'last_error', 'move_id.invoice_line_ids.tax_ids',
        'move_id.invoice_line_ids.tax_ids.amount', 'move_id.invoice_line_ids.tax_ids.type_code_id.code',
        'move_id.invoice_line_ids.tax_ids.sub_type_code_id.code', 'move_id.invoice_line_ids.tax_ids.l10n_eg_eta_code',
        'move_id.eta_vat_bypass_hash', 'move_id.partner_id', 'move_id.amount_total', 'move_id.state')
    def _compute_monitoring(self):
        super()._compute_monitoring()
        for job in self:
            if job.state == 'blocked' and 'VAT is missing' in (job.last_error or ''):
                if job.move_id._eta_exemption_codes():
                    job.staff_summary = _('An earlier attempt was stopped by the VAT check. This invoice has an explicit 0% VAT exemption and can now be retried.')
                    job.required_action = _('Click Sync on the invoice, or Send to ETA now. The exemption and invoice amounts will be preserved.')
                elif job.move_id._eta_vat_bypass_current():
                    job.staff_summary = _('An earlier attempt was stopped by the VAT check. Sending without VAT has since been approved for this invoice.')
                    job.required_action = _('Click Sync or Send to ETA now to continue with the recorded VAT bypass. All other ETA checks still apply.')

    def action_send_without_vat(self):
        self.ensure_one()
        self.check_access('read')
        return self.move_id.action_eta_send_without_vat()


class EtaVatBypass(models.TransientModel):
    _name = 'tradeline.eta.vat.bypass'
    _description = 'Send an invoice without VAT'
    _check_company_auto = True

    move_id = fields.Many2one('account.move', required=True, readonly=True, check_company=True)
    company_id = fields.Many2one(related='move_id.company_id')
    reason = fields.Text(required=True)

    def action_send(self):
        self.ensure_one()
        self.move_id._eta_approve_vat_bypass(self.reason)
        return self.move_id.action_eta_send_now()


class EtaExemptInvoiceLine(models.Model):
    _inherit = 'account.move.line'

    def _get_taxableItems(self, taxes_res):
        # Ahmed's serializer predates native tax-code metadata. Preserve an
        # explicitly selected zero-rate code instead of falling back to a
        # company-wide general-sales classification.
        if len(self.tax_ids) == 1 and exemption_subtype(self.tax_ids):
            return [{'taxType': 'T1', 'subType': exemption_subtype(self.tax_ids),
                'amount': 0.0, 'rate': 0.0}], 0.0
        return super()._get_taxableItems(taxes_res)

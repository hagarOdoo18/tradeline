"""Fresh government identity proof at the final document transmission boundary."""
import hashlib
import json
import re
from datetime import datetime

from odoo import fields, models, _
from odoo.exceptions import UserError

from .client import EtaError
from .historical_recovery import fiscal, fiscal_equal


def taxpayer(value):
    return re.sub(r'[^0-9]', '', str(value or ''))


class EtaSubmissionSafety(models.Model):
    _inherit = 'tradeline.eta.job'

    authority_receipt_hash = fields.Char(readonly=True)

    def _transmission_fingerprint(self):
        self.env.flush_all()
        self.env.cr.execute('''SELECT md5(jsonb_build_array(m.name,m.invoice_date,m.currency_id,
            m.partner_id,m.amount_total,m.amount_tax,m.amount_untaxed,m.amount_total_signed,
            m.amount_untaxed_signed,m.state,m.move_type,m.company_id,m.write_date,
            p.write_date,c.config_type,issuer.vat,issuer.write_date,
            (SELECT jsonb_agg(jsonb_build_array(l.id,l.write_date,v.write_date,t.write_date) ORDER BY l.id)
             FROM account_move_line l LEFT JOIN product_product v ON v.id=l.product_id
             LEFT JOIN product_template t ON t.id=v.product_tmpl_id WHERE l.move_id=m.id))::text)
            FROM account_move m JOIN res_partner p ON p.id=m.partner_id
            JOIN res_company c ON c.id=m.company_id JOIN res_partner issuer ON issuer.id=c.partner_id
            WHERE m.id=%s''', [self.move_id.id])
        return self.env.cr.fetchone()[0]

    def _assert_eta_number_unique(self):
        self.ensure_one()
        rin = taxpayer(self.company_id.vat)
        if not re.fullmatch(r'[0-9]{9}', rin):
            raise UserError(_('Configure the invoice company’s real nine-digit ETA taxpayer ID before submission.'))
        self.env.flush_all()
        self.env.cr.execute("""SELECT m.id FROM account_move m
            JOIN res_company c ON c.id=m.company_id JOIN res_partner p ON p.id=c.partner_id
            WHERE m.state='posted' AND m.move_type IN ('out_invoice','out_refund') AND m.name=%s
            AND c.config_type=%s AND regexp_replace(coalesce(p.vat,''),'[^0-9]','','g')=%s
            ORDER BY m.id LIMIT 2""", [self.move_id.name, self.company_id.config_type, rin])
        if [r[0] for r in self.env.cr.fetchall()] != [self.move_id.id]:
            raise UserError(_('This invoice number is repeated across Odoo companies using the same ETA taxpayer and environment. Finance must reconcile the records; no duplicate was sent.'))

    def _assert_eta_document_issuer(self, document):
        self.ensure_one()
        rin = taxpayer(self.company_id.vat)
        if (not isinstance(document, dict) or document.get('internalID') != self.move_id.name
                or not re.fullmatch(r'[0-9]{9}', rin)
                or taxpayer(document.get('issuer', {}).get('id')) != rin):
            raise UserError(_('The ETA document issuer or invoice number does not match this invoice company. IT and Finance must reconcile the original identity; nothing was submitted.'))

    @staticmethod
    def _receipt_set_hash(receipts):
        evidence = sorted([{'uuid': r.get('uuid'), 'status': r.get('status'),
                            'validation': r.get('validationResults')} for r in receipts], key=lambda r: r['uuid'] or '')
        return hashlib.sha256(json.dumps(evidence, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _history_candidates(self, client, proof):
        receipts = super()._history_candidates(client, proof)
        for receipt in receipts:
            document = receipt.get('document')
            if isinstance(document, str):
                document = json.loads(document)
            self._assert_eta_document_issuer(document)
        return receipts

    def _apply_remote(self, data):
        document = data.get('document')
        if isinstance(document, str):
            document = json.loads(document)
        if document is not None:
            self._assert_eta_document_issuer(document)
        return super()._apply_remote(data)

    def _preflight_submission(self, client):
        """Always GET before POST, including new invoices and prepared retries.

        A historical negative proof can expire before a queued document gets a
        turn. A fresh receipt set must agree with the exact proof that approved
        this retry; otherwise run normal reconciliation without transmitting.
        """
        self.ensure_one()
        self._assert_eta_number_unique()
        document = json.loads(self.payload)['documents'][0]
        self._assert_eta_document_issuer(document)
        if self.historical_fingerprint and self._historical_financial_fingerprint() != self.historical_fingerprint:
            raise UserError(_('Posted accounting changed after historical verification. Finance must review this invoice.'))
        current = self._submission_source()
        if not fiscal_equal(fiscal(document), fiscal(current)):
            raise UserError(_('The prepared ETA request differs from the current posted source. Reconcile it before submitting.'))
        proof = self._history_snapshot()
        if not proof:
            raise EtaError('Complete taxpayer-scoped authority history is required before ETA submission.')
        if datetime.combine(self.move_id.invoice_date, datetime.min.time()) < proof['from']:
            raise UserError(_('This invoice predates the complete authority-history proof. Expand the proof before submission.'))
        receipts = self._history_candidates(client, proof)
        digest = self._receipt_set_hash(receipts)
        if not receipts:
            self.authority_receipt_hash = digest
            return True
        if (self.historical_submission_verified and self.authority_receipt_hash == digest
                and all(r.get('status') == 'Invalid' for r in receipts)):
            return True
        self.write({'state': 'review', 'historical_review': True})
        self._review_historical(client, verified=(proof, receipts))
        return False


class EtaCompanySubmissionSafety(models.Model):
    _inherit = 'res.company'

    def test_call_e_invoice_api(self):
        raise UserError(_('Use a real posted invoice through the ETA queue. The legacy sample-document test cannot submit tax documents.'))

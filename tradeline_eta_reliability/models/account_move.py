import ast
import base64
import binascii
import hashlib
import json
import re
from datetime import datetime, time

import pytz
import requests
from asn1crypto import cms

from odoo import fields, models, _
from odoo.exceptions import AccessError, UserError

from .client import EtaClient, EtaError


class AccountMove(models.Model):
    _inherit = 'account.move'

    eta_job_ids = fields.One2many('tradeline.eta.job', 'move_id', string='ETA Submission Jobs')

    def _post(self, soft=True):
        posted = super(AccountMove, self.with_context(eta_new_invoice=True))._post(soft=soft)
        eligible = posted.filtered(lambda m: m.move_type in ('out_invoice', 'out_refund'))
        eligible._eta_enqueue()
        return posted

    def _eta_enqueue(self, reconcile=False):
        Job = self.env['tradeline.eta.job'].sudo()
        for move in self.filtered(lambda m: m.state == 'posted' and m.move_type in ('out_invoice', 'out_refund')):
            if not move.company_id.client_id or not move.company_id.client_secret:
                continue
            job = Job.search([('move_id', '=', move.id)], limit=1)
            if not job:
                values = {'move_id': move.id, 'state': 'accepted' if move.e_invoice_uuid else
                          'unknown' if reconcile or move.e_invoice_sent else 'queued'}
                if values['state'] == 'unknown':
                    values['first_submission_candidate'] = not any((
                        move.e_invoice_sent, move.e_invoice_json, move.invoice_signed,
                        move.l10n_eg_uuid, move.l10n_eg_submission_number, move.l10n_eg_eta_json_doc_id))
                if values['state'] == 'unknown' and move.e_invoice_json:
                    try:
                        document = ast.literal_eval(move.e_invoice_json)
                        if isinstance(document, bytes):
                            document = ast.literal_eval(document.decode())
                        if isinstance(document, dict):
                            payload = json.dumps({'documents': [document]}, ensure_ascii=False, separators=(',', ':'))
                            values.update(payload=payload, payload_hash=hashlib.sha256(payload.encode()).hexdigest())
                    except (ValueError, SyntaxError, TypeError):
                        pass
                Job.create(values)
            elif move.e_invoice_uuid and job.state not in ('valid', 'invalid', 'blocked'):
                job.write({'state': 'accepted'})

    def action_send_electronic_invoice(self):
        """Manual and legacy callers enter the same durable queue as the cron."""
        if any(m.state != 'posted' or m.move_type not in ('out_invoice', 'out_refund') for m in self):
            raise UserError(_('Only posted customer invoices and credit notes can be queued for ETA.'))
        self.check_access('write')
        for move in self.filtered(lambda m: m.e_invoice_uuid and m.e_invoice_status == 'Invalid'):
            move._eta_queue_corrected_document()
        self._eta_enqueue(reconcile=True)
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'title': _('ETA submission queued'),
            'message': _('Existing ETA IDs will be checked before any submission.'), 'type': 'info',
        }}

    def _eta_correction_document(self):
        self.ensure_one()
        return self.with_company(self.company_id).action_generate_eta_json()[0]

    def _get_eta_personal_details(self, partner, order_total):
        domestic_person = partner.company_type == 'person' and partner.mobile_type == 'local'
        if not domestic_person:
            return super()._get_eta_personal_details(partner, order_total)
        egp = self.env.ref('base.EGP')
        total_egp = abs(order_total)
        if len(self) == 1 and partner == self.partner_id and self.move_type in ('out_invoice', 'out_refund'):
            # Use the posted document amount, including discounts and taxes.
            # Company-currency balances preserve the original posted FX rate.
            if self.currency_id == egp:
                total_egp = abs(self.amount_total)
            elif self.company_id.currency_id == egp:
                total_egp = abs(self.amount_total_signed)
            else:
                total_egp = self.currency_id._convert(
                    abs(self.amount_total), egp, self.company_id,
                    self.invoice_date or fields.Date.context_today(self))
        # ETA reduced the inclusive threshold to EGP 25,000 on 1 August 2024:
        # https://www.eta.gov.eg/en/node/781
        if egp.compare_amounts(total_egp, 25000) >= 0:
            if not partner.vat:
                raise UserError(_('National ID is required for domestic individual invoices of EGP 25,000 or more.'))
            if not re.fullmatch(r'[0-9]{14}', partner.vat):
                raise UserError(_('A 14-digit national ID is required for domestic individual invoices of EGP 25,000 or more.'))
        # Keep the existing address, receiver type and optional recorded ID.
        # Only replace the obsolete EGP 50,000 preflight; never invent an ID.
        return super()._get_eta_personal_details(partner, 0)

    def generate_canonical_manual(self, invoice_params):
        # Use Odoo's ETA serializer: JSON string escaping is part of the signed
        # bytes, including quotation marks in inch sizes and control characters.
        return self.env['l10n_eg_edi.thumb.drive']._serialize_for_signing(invoice_params)

    def _eta_validate_signature_digest(self, signature):
        """Check binding to these bytes; ETA still validates certificate and RSA signature."""
        self.ensure_one()
        try:
            data = cms.ContentInfo.load(base64.b64decode(''.join(signature.split()), validate=True), strict=True)
            if data['content_type'].native != 'signed_data':
                raise ValueError('Expected CMS SignedData')
            signers = data['content']['signer_infos']
            if len(signers) != 1 or signers[0]['digest_algorithm']['algorithm'].native != 'sha256':
                raise ValueError('Expected one SHA256 signer')
            digests = [attribute['values'][0].native for attribute in signers[0]['signed_attrs']
                       if attribute['type'].native == 'message_digest']
            if digests != [hashlib.sha256(self.e_invoice_canonical.encode('utf-8')).digest()]:
                raise ValueError('Document digest mismatch')
        except (ValueError, TypeError, KeyError, IndexError, binascii.Error, AttributeError) as exc:
            raise UserError(_('The signing service returned an invalid signature or a digest that does not match this document. Nothing was sent to ETA; signing will be retried.')) from exc
        return True

    def _get_eta_invoice_lines(self):
        lines, discount, sales, total = super()._get_eta_invoice_lines()
        self.ensure_one()
        source = self.invoice_line_ids.filtered(lambda line: line.product_id and line.quantity)
        if len(source) != len(lines):
            raise UserError(_('The ETA lines do not match the invoice lines. Review the document before submitting.'))
        egp = self.currency_id == self.env.ref('base.EGP')
        for posted_line, eta_line in zip(source, lines):
            if not posted_line.price_unit and not posted_line.price_subtotal:
                # A free item has a zero unit value. The legacy fallback of 1
                # contradicted its zero SalesTotal and caused ETA SF330 errors.
                eta_line['unitValue'].update(amountEGP=0.0, amountSold=0.0)
            if egp and not any(tax.amount for tax in posted_line.tax_ids):
                # The legacy zero-tax branch used price * quantity and lost
                # the discount. Use the existing accounting amount instead.
                correct_total = round(posted_line.price_total, 4)
                total += correct_total - eta_line['total']
                eta_line['total'] = correct_total
        return lines, discount, sales, round(total, 4)

    def _check_extra_fields(self, invoice_params):
        invoice_params = super()._check_extra_fields(invoice_params)
        self.ensure_one()
        # The legacy generator uses date.today(). Keep the actual invoice date
        # in both the document and the canonical bytes generated immediately after this hook.
        if self.invoice_date:
            invoice_params['dateTimeIssued'] = self._eta_issue_timestamp()
        return invoice_params

    def _eta_issue_timestamp(self):
        """Represent the Egyptian invoice date in the UTC timestamp ETA requires."""
        self.ensure_one()
        job = self.eta_job_ids.sudo()
        if job and job.issue_date_policy == 'current':
            job._effective_issue_date()  # Require the recorded approval.
            if not job.current_issue_datetime:
                raise UserError(_('The queue must prepare the approved ETA issue time before generating this document.'))
            return job.current_issue_datetime.strftime('%Y-%m-%dT%H:%M:%SZ')
        cairo = pytz.timezone('Africa/Cairo')
        if self.create_date:
            created = pytz.utc.localize(self.create_date)
            if created.astimezone(cairo).date() == self.invoice_date:
                # Retain the stored issue-time evidence when it belongs to the
                # invoice's date; midnight would age a late-night invoice by hours.
                return created.strftime('%Y-%m-%dT%H:%M:%SZ')
        midnight = datetime.combine(self.invoice_date, time.min)
        try:
            issued = cairo.localize(midnight, is_dst=None)
        except pytz.AmbiguousTimeError:
            issued = cairo.localize(midnight, is_dst=True)
        except pytz.NonExistentTimeError:
            # On the spring clock change, this maps forward to the first valid
            # hour of the same Egyptian date rather than the previous date.
            issued = cairo.localize(midnight, is_dst=False)
        return issued.astimezone(pytz.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    def _eta_queue_corrected_document(self):
        """Explicit owner action; an unchanged or still-accepted document is never resent."""
        self.ensure_one()
        if not self.env.is_superuser() and not self.env.user.has_group('account.group_account_manager'):
            raise AccessError(_('An accounting manager must approve a corrected ETA submission.'))
        self._eta_enqueue()
        job = self.eta_job_ids.sudo().with_company(self.company_id)
        client = EtaClient(self.company_id.sudo())
        try:
            result = client.request('GET', '/api/v1.0/documents/%s/raw' % self.e_invoice_uuid)
        except (EtaError, requests.RequestException) as exc:
            raise UserError(_('ETA could not confirm the previous document status. Nothing was resubmitted.')) from exc
        finally:
            client.close()
        if result.get('uuid') != self.e_invoice_uuid or result.get('internalId') != self.name:
            raise UserError(_('ETA returned a different document. Review its identity before resubmitting.'))
        if result.get('status') != 'Invalid':
            job._apply_remote(result)
            return
        if 'DS301' in json.dumps(result.get('validationResults') or {}):
            raise UserError(_('ETA identifies this as a duplicate. Reconcile the referenced original document before any corrected submission.'))
        original = result.get('document')
        if isinstance(original, str):
            try:
                original = json.loads(original)
            except ValueError:
                original = None
        if not isinstance(original, dict):
            raise UserError(_('The rejected document could not be verified. Nothing was resubmitted.'))
        corrected = self._eta_correction_document()
        def content(document):
            return {key: value for key, value in document.items() if key not in ('dateTimeIssued', 'signatures')}
        def leaf_codes(node):
            if isinstance(node, list):
                return [code for item in node for code in leaf_codes(item)]
            if isinstance(node, dict):
                children = [code for value in node.values() if isinstance(value, (dict, list))
                            for code in leaf_codes(value)]
                code = node.get('errorCode') or node.get('code')
                return children or ([str(code)] if code else [])
            return []
        rejected_codes = leaf_codes(result.get('validationResults') or {})
        signature_only = bool(rejected_codes) and set(rejected_codes) == {'4043'}
        if content(original) == content(corrected) and not signature_only:
            raise UserError(_('Correct the rejected customer, product or tax details first. Changing the issue date alone cannot retry this document.'))
        # Keep the old government document and UUID before queuing a corrected
        # version. No signing or submission HTTP call occurs in this transaction.
        self.env['ir.attachment'].sudo().create({
            'name': 'ETA_rejected_%s.json' % self.e_invoice_uuid,
            'res_model': self._name, 'res_id': self.id, 'company_id': self.company_id.id,
            'mimetype': 'application/json', 'raw': json.dumps(result, ensure_ascii=False).encode(),
        })
        self.write({'e_invoice_uuid': False, 'e_invoice_long_id': False, 'e_invoice_date': False,
                    'e_invoice_sent': False, 'e_invoice_status': 'Draft', 'eta_submission_pending': True,
                    'l10n_eg_eta_json_doc_id': False, 'invoice_signed': False})
        job.write({'state': 'queued', 'payload': False, 'attempted_at': False, 'submission_uuid': False,
                   'next_attempt': False, 'safe_to_retry': False, 'first_submission_candidate': False,
                   'last_error': False, 'failures': 0})

    def action_update_electronic_invoice_status(self):
        self.check_access('write')
        self._eta_enqueue()
        jobs = self.env['tradeline.eta.job'].sudo().search([
            ('move_id', 'in', self.ids), ('move_id.e_invoice_uuid', '!=', False)])
        jobs.write({'state': 'accepted', 'next_attempt': False})
        return True

    def cron_send_invoices(self):
        return self.env['tradeline.eta.job']._cron_process('submit')

    def post(self):
        # Legacy entry point must not erase already assigned ETA identifiers.
        return self.action_post()

    def get_signature_value(self):
        """Signing never commits an invoice transaction or releases its locks."""
        self.ensure_one()
        company = self.company_id.sudo()
        if company.signature_tool != 'python':
            raise UserError(_('Automatic ETA signing currently requires the configured Python signer.'))
        if not self.e_invoice_canonical:
            self.with_company(company).action_generate_eta_json()
        try:
            result = requests.get(company.signature_url, data={
                'data': self.e_invoice_canonical, 'token_label': company.signature_label,
                'user_pin': company.signature_pin,
            }, timeout=(5, 25))
            if result.status_code != 200 or not result.text.strip():
                raise UserError(_('The ETA signer did not return a signature (HTTP %s).', result.status_code))
        except requests.RequestException as exc:
            raise UserError(_('The ETA signer is unavailable; the queue will retry.')) from exc
        self._eta_validate_signature_digest(result.text.strip())
        self.static_signature = result.text.strip()
        return self.static_signature

    def _eta_native_result(self):
        self.ensure_one()
        if self.e_invoice_uuid and self.e_invoice_status == 'Valid':
            return {'success': True}
        if self.e_invoice_uuid and self.e_invoice_status in ('Invalid', 'Rejected', 'Cancelled'):
            return {'error': _('ETA reports %s. Correct the document before any resubmission.', self.e_invoice_status),
                    'blocking_level': 'error'}
        self._eta_enqueue(reconcile=not bool(self.env.context.get('eta_new_invoice')))
        return {'error': _('The Tradeline ETA queue is processing this invoice. See ETA Submission Jobs.'),
                'blocking_level': 'info'}

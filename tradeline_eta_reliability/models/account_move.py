import ast
import hashlib
import json
from datetime import datetime, time

import pytz
import requests

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
        if content(original) == content(corrected):
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

import ast
import base64
import binascii
import hashlib
import json
import re
from datetime import datetime, time
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

import pytz
import requests
from asn1crypto import cms

from odoo import fields, models, _
from odoo.exceptions import AccessError, UserError

from .client import EtaClient, EtaError
from .processing_window import check_processing_window


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
        details = super()._get_eta_personal_details(partner, 0)
        # ETA validates an optional value when supplied. A placeholder such as
        # 1 must not turn an otherwise eligible small personal sale into a
        # rejection. A well-formed recorded ID is kept unless ETA rejected it
        # and complete receipt reconciliation approved the optional omission.
        below = egp.compare_amounts(total_egp, 25000) < 0
        recorded = details.get('id') or ''
        malformed = (not re.fullmatch(r'[0-9]{14}', recorded) or len(set(recorded)) == 1)
        job_policy = (bool(self.eta_job_ids.sudo().omit_optional_receiver_id)
                      if len(self) == 1 and partner == self.partner_id else False)
        if (below and details.get('type') == 'P' and details.get('address', {}).get('country') == 'EG'
                and not self.env.context.get('eta_keep_recorded_person_id') and (malformed or job_policy)):
            details['id'] = ''
        return details

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
        if self.state == 'posted' and self.move_type in ('out_invoice', 'out_refund'):
            expected_net, expected_total = self._eta_posted_amounts()
            if (self.currency_id != self.env.ref('base.EGP')
                    or abs(invoice_params['netAmount'] - expected_net) >= .005
                    or abs(invoice_params['totalAmount'] - expected_total) >= .005):
                invoice_params = self._eta_reconcile_posted_document(invoice_params)
        return invoice_params

    def _eta_posted_amounts(self):
        """ETA is denominated in EGP, including when the sale currency differs."""
        self.ensure_one()
        if self.currency_id == self.env.ref('base.EGP'):
            return abs(self.amount_untaxed), abs(self.amount_total)
        if self.company_id.currency_id == self.env.ref('base.EGP'):
            return abs(self.amount_untaxed_signed), abs(self.amount_total_signed)
        raise UserError(_('Historical foreign-currency recovery requires posted EGP accounting balances.'))

    def _eta_reconcile_posted_document(self, document):
        """Replace only monetary calculations, using the immutable posted ledger."""
        self.ensure_one()
        D = lambda value: Decimal(str(value))
        Q = lambda value: float(D(value).quantize(Decimal('.00001'), rounding=ROUND_HALF_UP))
        if self.company_id.currency_id != self.env.ref('base.EGP'):
            raise UserError(_('Posted EGP balances are required to reconcile ETA calculations.'))
        source = self.invoice_line_ids.filtered(lambda line: line.product_id and line.quantity)
        if len(source) != len(document['invoiceLines']):
            raise UserError(_('ETA product lines differ from posted invoice lines.'))
        direction = -1 if self.move_type == 'out_invoice' else 1
        if any(line.quantity <= 0 or line.balance * direction < 0 or not 0 <= line.discount <= 100 for line in source):
            raise UserError(_('Mixed-sign invoice lines require explicit ETA accounting reconciliation.'))
        details = self._prepare_invoice_aggregated_taxes(round_from_tax_lines=True)
        # Tax journal items are global. Keep each posted tax total, while moving
        # only the rounding residual between its own lines to satisfy ETA's
        # one-cent monetary tolerance. Never alter a posted journal item.
        vat_groups = defaultdict(list)
        for posted in source:
            for tax, values in details['tax_details_per_record'].get(posted, {}).get('tax_details', {}).items():
                if tax.type_code_id.code == 'T1' and tax.amount_type == 'percent' and tax.amount >= 0:
                    expected = D(Q(abs(D(posted.balance)) * D(tax.amount) / 100))
                    original = abs(D(Q(values['tax_amount'])))
                    lower = max(D(0), expected - D('.009')); upper = expected + D('.009')
                    vat_groups[tax.id].append([values, original, lower, upper, min(upper, max(lower, original))])
        for entries in vat_groups.values():
            residual = sum((entry[1] - entry[4] for entry in entries), D(0))
            for entry in sorted(entries, key=lambda entry: entry[3] - entry[2], reverse=True):
                if residual > 0:
                    shift = min(residual, entry[3] - entry[4])
                else:
                    shift = -min(-residual, entry[4] - entry[2])
                entry[4] += shift; residual -= shift
            if abs(residual) > D('.000001'):
                raise UserError(_('Posted VAT differs from its recorded tax rate. Finance must reconcile the posted tax ledger.'))
            for values, _old, _lower, _upper, amount in entries:
                values['tax_amount'] = Q(amount)
        foreign = self.currency_id != self.env.ref('base.EGP')
        nonzero = source.filtered(lambda line: line.amount_currency)
        if foreign:
            rate = (sum(abs(D(line.balance)) for line in nonzero) / sum(abs(D(line.amount_currency)) for line in nonzero)
                    if nonzero else D(1) / D(self.invoice_currency_rate))
            rate = D(Q(rate))
        else:
            rate = D(1)
        if rate <= 0:
            raise UserError(_('The original posted exchange rate is missing.'))
        totals = defaultdict(Decimal); type_totals = defaultdict(Decimal)
        for posted, line in zip(source, document['invoiceLines']):
            if line['quantity'] != posted.quantity:
                raise UserError(_('ETA quantities differ from the posted invoice.'))
            net = abs(D(posted.balance)); qty = D(posted.quantity); discount = D(posted.discount)
            if foreign and abs(abs(D(posted.amount_currency)) * rate - net) > D('.01'):
                raise UserError(_('Posted exchange rates differ between invoice lines. Reconcile the recorded FX before submission.'))
            if discount < 100:
                sales = net / (1 - discount / 100)
                sold_sales = abs(D(posted.price_subtotal)) / (1 - discount / 100)
            else:
                uncut = posted.tax_ids.compute_all(posted.price_unit, currency=self.currency_id,
                    quantity=posted.quantity, product=posted.product_id, partner=self.partner_id)['total_excluded']
                sold_sales = abs(D(uncut)); sales = sold_sales * rate
            amount_egp = D(Q(sales / qty)); amount_sold = D(Q(sold_sales / qty))
            if abs(amount_egp * qty - sales) > D('.009') or foreign and abs(amount_sold * rate - amount_egp) > D('.009'):
                raise UserError(_('ETA five-decimal unit precision does not reconcile this quantity and posted FX. IT must review the calculation.'))
            taxable = []; signed_tax = D(0)
            allocated = details['tax_details_per_record'].get(posted, {}).get('tax_details', {})
            for tax, values in allocated.items():
                code = tax.type_code_id.code; sub = tax.sub_type_code_id.code
                if (tax.amount_type != 'percent' or code not in ('T1', 'T4') or not sub
                        or tax.is_deduction or code == 'T1' and tax.amount < 0 or code == 'T4' and tax.amount > 0):
                    raise UserError(_('This posted tax combination requires explicit ETA calculation review.'))
                amount = abs(D(Q(values['tax_amount'])))
                if code == 'T1' and abs(amount - net * abs(D(tax.amount)) / 100) > D('.00901'):
                    raise UserError(_('Posted VAT differs from its recorded tax rate. Finance must reconcile the posted tax ledger.'))
                if any(item['taxType'] == code for item in taxable):
                    raise UserError(_('Multiple tax rates share one ETA tax type on an invoice line.'))
                taxable.append({'taxType': code, 'amount': Q(amount), 'subType': sub, 'rate': Q(abs(D(tax.amount)))})
                type_totals[code] += amount
                signed_tax += -amount if code == 'T4' else amount
            # An untaxed item keeps its recorded zero-tax classification, if any.
            if not taxable:
                taxable = [item for item in line.get('taxableItems', []) if not item.get('amount') and not item.get('rate')]
            line.update(salesTotal=Q(sales), netTotal=Q(net), total=Q(net + signed_tax),
                        discount={'rate': Q(discount), 'amount': Q(sales - net)}, taxableItems=taxable)
            line['unitValue'] = {'currencySold': self.currency_id.name, 'amountEGP': Q(amount_egp)}
            if foreign:
                line['unitValue'].update(amountSold=Q(amount_sold), currencyExchangeRate=Q(rate))
            totals['sales'] += D(line['salesTotal']); totals['discount'] += D(line['discount']['amount'])
            totals['net'] += D(line['netTotal']); totals['total'] += D(line['total'])
        ledger = defaultdict(Decimal)
        for tax_line in self.line_ids.filtered('tax_line_id'):
            code = tax_line.tax_line_id.type_code_id.code
            ledger[code] += abs(D(tax_line.balance))
        if any(abs(type_totals[key] - ledger[key]) >= D('.005') for key in set(ledger) | set(type_totals)):
            raise UserError(_('ETA tax allocations do not reconcile the posted tax ledger.'))
        expected_net, expected_total = self._eta_posted_amounts()
        if abs(totals['net'] - D(expected_net)) >= D('.005') or abs(totals['total'] - D(expected_total)) >= D('.005'):
            raise UserError(_('ETA product coverage does not reconcile posted accounting values. Check non-product lines and manual adjustments.'))
        document.update(totalSalesAmount=Q(totals['sales']), totalDiscountAmount=Q(totals['discount']),
                        netAmount=Q(totals['net']), totalAmount=Q(totals['total']),
                        taxTotals=([{'taxType': key, 'amount': Q(value)} for key, value in type_totals.items()]
                                   or [item for item in document.get('taxTotals', []) if not item.get('amount')]))
        return document

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
            ('move_id', 'in', self.ids), '|',
            ('move_id.e_invoice_uuid', '!=', False), ('move_id.l10n_eg_uuid', '!=', False)])
        # Use the same full-history routing as the log's Refresh action. Polling
        # one UUID must not erase a duplicate warning or a held source comparison.
        jobs.action_recheck()
        return True

    def cron_send_invoices(self):
        return self.env['tradeline.eta.job']._cron_process('submit')

    def post(self):
        # Legacy entry point must not erase already assigned ETA identifiers.
        return self.action_post()

    def get_signature_value(self):
        """Signing never commits an invoice transaction or releases its locks."""
        self.ensure_one()
        check_processing_window(self.env)
        company = self.company_id.sudo()
        if company.signature_tool != 'python':
            raise UserError(_('Automatic ETA signing currently requires the configured Python signer.'))
        if not self.e_invoice_canonical:
            self.with_company(company).action_generate_eta_json()
        try:
            check_processing_window(self.env)
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
        if not self.company_id.client_id or not self.company_id.client_secret:
            return {'error': _('Configure ETA credentials on this invoice company. The native sender will not submit a second copy.'),
                    'blocking_level': 'error'}
        self._eta_enqueue(reconcile=not bool(self.env.context.get('eta_new_invoice')))
        return {'error': _('The Tradeline ETA queue is processing this invoice. See ETA Submission Jobs.'),
                'blocking_level': 'info'}

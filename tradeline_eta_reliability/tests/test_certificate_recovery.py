import base64
import hashlib
import json
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from asn1crypto import cms, x509 as asn1_x509
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from ..models.certificate_recovery import signer_certificate
from ..models.client import EtaError


@tagged('post_install', '-at_install')
class TestCertificateRecovery(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        values = {'vat': '123456789'}
        for name, value in [('company_size', 'small'), ('company_device', 1)]:
            if name in cls.env['res.partner']._fields:
                values[name] = value
        cls.company.partner_id.with_context(no_vat_validation=True).write(values)
        cls.env.ref('base.EGP').active = True
        cls.company.write({'client_id': 'certificate-qa', 'client_secret': 'qa-only-secret',
            'config_type': 'uat', 'config_version': '1.0', 'signature_tool': 'python',
            'signature_url': 'https://signer.invalid/qa-only', 'signature_label': 'QA only', 'signature_pin': 'qa-only'})
        cls.receivable = cls.env['account.account'].create({'name': 'Certificate QA receivable', 'code': 'CERTAR',
            'account_type': 'asset_receivable', 'reconcile': True, 'company_ids': [Command.set(cls.company.ids)]})
        cls.income = cls.env['account.account'].create({'name': 'Certificate QA income', 'code': 'CERTINC',
            'account_type': 'income', 'company_ids': [Command.set(cls.company.ids)]})
        cls.partner = cls.env['res.partner'].create({'name': 'Certificate QA buyer',
            'property_account_receivable_id': cls.receivable.id})
        cls.journal = cls.env['account.journal'].create({'name': 'Certificate QA sales', 'code': 'CERT',
            'type': 'sale', 'company_id': cls.company.id, 'edi_format_ids': [Command.clear()]})
        now = datetime.now(timezone.utc)
        cls.old_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.new_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.old_cert = cls.certificate(cls.old_key, 101, now - timedelta(days=365), now - timedelta(days=100))
        cls.new_cert = cls.certificate(cls.new_key, 102, now - timedelta(days=1), now + timedelta(days=90))
        cls.future_cert = cls.certificate(cls.new_key, 103, now + timedelta(days=1), now + timedelta(days=90))

    @staticmethod
    def certificate(key, serial, before, after):
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Ephemeral QA signer only')])
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(serial).not_valid_before(before).not_valid_after(after)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
        return asn1_x509.Certificate.load(cert.public_bytes(serialization.Encoding.DER))

    @staticmethod
    def canonical(document):
        return json.dumps({k: v for k, v in document.items() if k != 'signatures'}, sort_keys=True, separators=(',', ':'))

    @staticmethod
    def signature(canonical, certificate, key, extra=(), ski=False, signing_time=None, corrupt=False):
        signing_time = signing_time or datetime.now(timezone.utc)
        attributes = cms.CMSAttributes([
            {'type': 'content_type', 'values': ['data']},
            {'type': 'message_digest', 'values': [hashlib.sha256(canonical.encode()).digest()]},
            {'type': 'signing_time', 'values': [cms.Time({'utc_time': signing_time})]}])
        sid = ({'subject_key_identifier': certificate.key_identifier} if ski else
               {'issuer_and_serial_number': {'issuer': certificate.issuer, 'serial_number': certificate.serial_number}})
        signed = key.sign(attributes.dump(), padding.PKCS1v15(), hashes.SHA256())
        if corrupt:
            signed = signed[:-1] + bytes([signed[-1] ^ 1])
        content = cms.ContentInfo({'content_type': 'signed_data', 'content': {
            'version': 'v3' if ski else 'v1', 'digest_algorithms': [{'algorithm': 'sha256'}],
            'encap_content_info': {'content_type': 'data'}, 'certificates': [certificate, *extra],
            'signer_infos': [{'version': 'v3' if ski else 'v1', 'sid': sid,
                'digest_algorithm': {'algorithm': 'sha256'}, 'signed_attrs': attributes,
                'signature_algorithm': {'algorithm': 'rsassa_pkcs1v15'}, 'signature': signed}]}})
        return base64.b64encode(content.dump()).decode()

    def setUp(self):
        super().setUp()
        params = self.env['ir.config_parameter'].sudo()
        for suffix in ('enabled_database', 'historical_recovery_database'):
            params.set_param('tradeline_eta_reliability.' + suffix, self.env.cr.dbname)
        self.move = self.env['account.move'].create({'move_type': 'out_invoice', 'journal_id': self.journal.id,
            'currency_id': self.env.ref('base.EGP').id, 'partner_id': self.partner.id,
            'invoice_date': fields.Date.today() - timedelta(days=60),
            'invoice_line_ids': [Command.create({'name': 'Certificate QA item', 'account_id': self.income.id,
                'quantity': 1, 'price_unit': 100, 'tax_ids': [Command.clear()]})]})
        self.move.action_post(); self.job = self.move.eta_job_ids
        self.source = {'internalID': self.move.name, 'dateTimeIssued': str(self.move.invoice_date) + 'T00:00:00Z',
            'documentType': 'I', 'issuer': {'id': '123456789', 'address': {'country': 'EG'}},
            'receiver': {'type': 'P', 'id': '', 'name': self.partner.name, 'address': {'country': 'EG'}},
            'invoiceLines': [{'itemCode': 'QA-CERT', 'internalCode': 'QA-CERT', 'itemType': 'EGS',
                'description': 'Certificate QA item', 'quantity': 1,
                'unitValue': {'currencySold': 'EGP', 'amountEGP': 100}, 'total': 100}],
            'totalAmount': 100, 'netAmount': 100, 'taxTotals': []}
        original = deepcopy(self.source)
        original['signatures'] = [{'signatureType': 'I', 'value': self.signature(self.canonical(original),
            self.old_cert, self.old_key, signing_time=datetime.now(timezone.utc) - timedelta(days=60))}]
        self.raw = {'uuid': 'expired-certificate-original', 'internalId': self.move.name, 'status': 'Invalid',
            'dateTimeReceived': (datetime.now(timezone.utc) - timedelta(days=60)).isoformat(), 'document': original,
            'validationResults': {'errorCode': 'Err03', 'innerError': [{'errorCode': '4159',
                'propertyPath': 'documents.signatures.value', 'error': 'The certificate was expired'}]}}
        self.receipts = [self.raw]
        self.move.write({'e_invoice_uuid': self.raw['uuid'], 'e_invoice_status': 'Invalid', 'e_invoice_sent': True,
            'e_invoice_json': repr(original), 'e_invoice_canonical': self.canonical(original),
            'static_signature': original['signatures'][0]['value'], 'invoice_signed': True})
        self.job.write({'state': 'invalid', 'last_error': json.dumps(self.raw['validationResults']),
            'attempted_at': datetime.fromisoformat(self.raw['dateTimeReceived']).replace(tzinfo=None) - timedelta(seconds=1),
            'payload': json.dumps({'documents': [original]})})
        self.current_cert = self.new_cert; self.current_key = self.new_key

    @contextmanager
    def routes(self):
        history = {'from': fields.Datetime.now() - timedelta(days=400),
                   'to': fields.Datetime.now() - timedelta(minutes=1), 'receipts': {}}
        def generate():
            document = deepcopy(self.source)
            issued = self.job.current_issue_datetime
            document['dateTimeIssued'] = issued.replace(tzinfo=timezone.utc).isoformat() if issued else document['dateTimeIssued']
            self.move.write({'e_invoice_json': repr(document), 'e_invoice_canonical': self.canonical(document), 'invoice_signed': False})
        def sign(_url, data, **_kwargs):
            return SimpleNamespace(status_code=200,
                text=self.signature(data['data'], self.current_cert, self.current_key))
        client = MagicMock()
        with ExitStack() as stack:
            stack.enter_context(patch('requests.sessions.Session.request', side_effect=AssertionError('Unexpected external request')))
            signer = stack.enter_context(patch('odoo.addons.tradeline_eta_reliability.models.account_move.requests.get', side_effect=sign))
            stack.enter_context(patch.object(type(self.move), 'action_generate_eta_json', side_effect=generate))
            stack.enter_context(patch.object(type(self.job), '_history_snapshot', return_value=history))
            candidates = stack.enter_context(patch.object(type(self.job), '_history_candidates', return_value=self.receipts))
            stack.enter_context(patch.object(type(self.job), '_historical_source', return_value=self.source))
            stack.enter_context(patch.object(type(self.job), '_submission_source', return_value=self.source))
            stack.enter_context(patch.object(type(self.job), '_historical_matches', return_value=True))
            yield client, candidates, signer

    def test_actual_signer_is_selected_by_issuer_serial_and_ski_not_arbitrary_certificate(self):
        now = datetime.now(timezone.utc)
        for ski in (False, True):
            with self.subTest(ski=ski):
                signature = self.signature('canonical', self.new_cert, self.new_key, extra=[self.old_cert], ski=ski)
                result = signer_certificate(signature, 'canonical', valid_at=[now])
                self.assertEqual(result['fingerprint'], hashlib.sha256(self.new_cert.dump()).hexdigest())
        expired_with_valid_decoy = self.signature('canonical', self.old_cert, self.old_key, extra=[self.new_cert])
        with self.assertRaisesRegex(ValueError, 'expired'):
            signer_certificate(expired_with_valid_decoy, 'canonical', valid_at=[now])

    def test_certificate_proof_rejects_digest_tampering_rsa_tampering_and_future_certificate(self):
        good = self.signature('canonical', self.new_cert, self.new_key)
        for value, canonical in [(good, 'other bytes'),
                (self.signature('canonical', self.new_cert, self.new_key, corrupt=True), 'canonical'),
                (self.signature('canonical', self.future_cert, self.new_key), 'canonical')]:
            with self.subTest(canonical=canonical), self.assertRaises(ValueError):
                signer_certificate(value, canonical, valid_at=[datetime.now(timezone.utc)])

    def test_legacy_naive_utc_certificate_api_keeps_exact_validity_bounds(self):
        actual = x509.load_der_x509_certificate(self.new_cert.dump())
        validity = self.new_cert['tbs_certificate']['validity']
        before = validity['not_before'].native
        after = validity['not_after'].native
        legacy = SimpleNamespace(public_key=actual.public_key,
            not_valid_before=before.replace(tzinfo=None), not_valid_after=after.replace(tzinfo=None))
        signed = self.signature('canonical', self.new_cert, self.new_key)
        with patch('odoo.addons.tradeline_eta_reliability.models.certificate_recovery.x509.load_der_x509_certificate',
                   return_value=legacy):
            result = signer_certificate(signed, 'canonical', valid_at=[before, after])
            self.assertEqual(result['not_before'], before.isoformat())
            self.assertEqual(result['not_after'], after.isoformat())
            for outside in (before - timedelta(microseconds=1), after + timedelta(microseconds=1)):
                with self.subTest(outside=outside), self.assertRaisesRegex(ValueError, 'expired or not yet valid'):
                    signer_certificate(signed, 'canonical', valid_at=[outside])

    def test_aware_certificate_api_is_preferred_and_normalized_without_relaxing_bounds(self):
        actual = x509.load_der_x509_certificate(self.new_cert.dump())
        validity = self.new_cert['tbs_certificate']['validity']
        before = validity['not_before'].native
        after = validity['not_after'].native
        offset = timezone(timedelta(hours=2))
        # No legacy properties are provided: using the wrong API must fail.
        aware = SimpleNamespace(public_key=actual.public_key,
            not_valid_before_utc=before.astimezone(offset), not_valid_after_utc=after.astimezone(offset))
        signed = self.signature('canonical', self.new_cert, self.new_key)
        with patch('odoo.addons.tradeline_eta_reliability.models.certificate_recovery.x509.load_der_x509_certificate',
                   return_value=aware):
            result = signer_certificate(signed, 'canonical', valid_at=[before, after])
            self.assertEqual(result['not_before'], before.isoformat())
            self.assertEqual(result['not_after'], after.isoformat())
            for outside in (before - timedelta(microseconds=1), after + timedelta(microseconds=1)):
                with self.subTest(outside=outside), self.assertRaisesRegex(ValueError, 'expired or not yet valid'):
                    signer_certificate(signed, 'canonical', valid_at=[outside])
            aware.not_valid_before_utc = before.replace(tzinfo=None)
            with self.assertRaisesRegex(ValueError, 'explicit UTC'):
                signer_certificate(signed, 'canonical', valid_at=[before])

    def test_unproved_expired_certificate_is_not_added_to_a_general_retry_allowlist(self):
        with self.routes() as (client, _candidates, signer):
            self.job._review_historical(client)
        self.assertEqual(self.job.state, 'invalid')
        signer.assert_not_called(); client.request.assert_not_called()
        self.assertEqual(self.move.e_invoice_uuid, self.raw['uuid'])

    def test_explicit_fresh_certificate_proof_rolls_back_preview_and_retains_all_original_evidence(self):
        original = (self.move.invoice_date, self.move.date, self.move.amount_total, self.move.amount_tax,
            self.move.e_invoice_uuid, self.move.e_invoice_json, self.move.e_invoice_canonical,
            self.move.static_signature, self.move.partner_id.vat, self.job.payload, self.job.attempted_at)
        with self.routes() as (client, _candidates, signer):
            self.assertTrue(self.job._prove_expired_certificate_recovery(client))
        self.assertEqual(self.job.state, 'review')
        self.assertEqual(original, (self.move.invoice_date, self.move.date, self.move.amount_total, self.move.amount_tax,
            self.move.e_invoice_uuid, self.move.e_invoice_json, self.move.e_invoice_canonical,
            self.move.static_signature, self.move.partner_id.vat, self.job.payload, self.job.attempted_at))
        self.assertEqual(signer.call_count, 1); client.request.assert_not_called()
        proof = self.job._certificate_recovery_proof()
        self.assertEqual(proof['document_posts'], 0)
        self.assertEqual(proof['original_receipts'], self.receipts)
        self.assertNotIn(proof['fresh_certificate']['fingerprint'], proof['expired_certificate_fingerprints'])

    def test_proved_certificate_retry_uses_current_approved_issue_time_and_final_history_preflight(self):
        before = (self.move.invoice_date, self.move.date, self.move.amount_total, self.move.amount_tax, self.partner.vat)
        with self.routes() as (client, candidates, signer):
            self.job._prove_expired_certificate_recovery(client)
            self.job._review_historical(client)
            self.assertEqual(self.job.state, 'queued')
            self.assertEqual(self.job.issue_date_policy, 'current')
            self.assertTrue(self.job.issue_date_approved_by and self.job.issue_date_approved_at)
            self.job._prepare()
            self.assertTrue(self.job._preflight_submission(client))
            self.assertGreaterEqual(candidates.call_count, 3)
            self.assertEqual(signer.call_count, 2)
        self.assertEqual(before, (self.move.invoice_date, self.move.date, self.move.amount_total, self.move.amount_tax, self.partner.vat))
        self.assertFalse(self.job.attempted_at)
        self.assertTrue(self.env['ir.attachment'].search_count([('res_model', '=', 'account.move'),
            ('res_id', '=', self.move.id), ('name', '=', 'ETA_historical_recovery_%s.json' % self.job.id)]))
        client.request.assert_not_called()

    def test_valid_pending_cancelled_or_mixed_error_receipts_prevent_certificate_proof(self):
        for status in ('Valid', 'Submitted', 'Cancelled'):
            with self.subTest(status=status), self.routes() as (client, candidates, signer):
                candidates.return_value = [dict(self.raw, status=status)]
                with self.assertRaisesRegex(UserError, 'exclusively Invalid'):
                    self.job._prove_expired_certificate_recovery(client)
                signer.assert_not_called(); client.request.assert_not_called()
        mixed = deepcopy(self.raw)
        mixed['validationResults']['innerError'].append({'errorCode': 'NI303'})
        with self.routes() as (client, candidates, signer):
            candidates.return_value = [mixed]
            with self.assertRaisesRegex(UserError, 'only expired-certificate'):
                self.job._prove_expired_certificate_recovery(client)
            signer.assert_not_called(); client.request.assert_not_called()
        self.assertFalse(self.job._certificate_recovery_proof())

    def test_stale_or_expired_signer_and_changed_fiscal_source_cannot_create_proof(self):
        for certificate, key in ((self.old_cert, self.old_key), (self.future_cert, self.new_key)):
            with self.subTest(certificate=certificate.serial_number), self.routes() as (client, _candidates, _signer):
                self.current_cert, self.current_key = certificate, key
                with self.assertRaisesRegex(UserError, 'proof failed'):
                    self.job._prove_expired_certificate_recovery(client)
                self.assertFalse(self.job._certificate_recovery_proof())
        self.current_cert, self.current_key = self.new_cert, self.new_key
        original_receipt = deepcopy(self.raw)
        before = (self.move.invoice_date, self.move.date, self.move.amount_total, self.move.amount_tax,
            self.move.e_invoice_uuid, self.move.e_invoice_json, self.move.static_signature,
            self.job.state, self.job.payload, self.job.attempted_at)
        # Keep the retained signed bytes correlated to their genuine Invalid
        # receipt. Change only the current preview to reach the fiscal guard.
        self.source['totalAmount'] = 101
        with self.routes() as (client, _candidates, signer):
            with self.assertRaisesRegex(UserError, 'differs from the current fiscal source'):
                self.job._prove_expired_certificate_recovery(client)
            signer.assert_not_called(); client.request.assert_not_called()
        self.assertFalse(self.job._certificate_recovery_proof())
        self.assertEqual(self.raw, original_receipt)
        self.assertEqual(before, (self.move.invoice_date, self.move.date, self.move.amount_total, self.move.amount_tax,
            self.move.e_invoice_uuid, self.move.e_invoice_json, self.move.static_signature,
            self.job.state, self.job.payload, self.job.attempted_at))

    def test_proof_expiration_or_signer_configuration_change_stops_before_resigning(self):
        with self.routes() as (client, _candidates, signer):
            self.job._prove_expired_certificate_recovery(client)
            self.job._review_historical(client)
            attachment = self.job._certificate_proof_attachments()[:1]
            proof = json.loads(attachment.raw)
            proof['created_utc'] = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()
            attachment.raw = json.dumps(proof).encode()
            with self.assertRaisesRegex(UserError, 'expired or its source/configuration changed'):
                self.job._prepare()
            self.assertEqual(signer.call_count, 1)
            proof['created_utc'] = datetime.now(timezone.utc).isoformat()
            attachment.raw = json.dumps(proof).encode()
            self.company.signature_label = 'Changed QA route'
            with self.assertRaisesRegex(UserError, 'source/configuration changed'):
                self.job._prepare()
            self.assertEqual(signer.call_count, 1)

    def test_late_approval_after_certificate_proof_is_restored_without_post(self):
        with self.routes() as (client, candidates, _signer):
            self.job._prove_expired_certificate_recovery(client)
            self.job._review_historical(client)
            self.job._prepare()
            approved = deepcopy(self.raw)
            approved.update(uuid='late-valid-after-certificate-proof', status='Valid',
                dateTimeReceived=datetime.now(timezone.utc).isoformat(),
                document=json.loads(self.job.payload)['documents'][0])
            candidates.return_value = [approved]
            self.assertFalse(self.job._preflight_submission(client))
            self.assertEqual(self.job.state, 'valid')
            self.assertEqual(self.move.e_invoice_uuid, approved['uuid'])
            client.request.assert_not_called()

    def test_same_certificate_cannot_be_reproved_after_new_authority_rejection(self):
        with self.routes() as (client, _candidates, _signer):
            self.job._prove_expired_certificate_recovery(client)
        self.raw['dateTimeReceived'] = datetime.now(timezone.utc).isoformat()
        with self.routes() as (client, _candidates, _signer):
            with self.assertRaisesRegex(UserError, 'already rejected this replacement certificate'):
                self.job._prove_expired_certificate_recovery(client)
        self.assertEqual(len(self.job._certificate_proof_attachments()), 1)

    def test_final_payload_cannot_reuse_an_expired_original_signature(self):
        with self.routes() as (client, _candidates, _signer):
            self.job._prove_expired_certificate_recovery(client)
            self.job._review_historical(client)
            self.job._prepare()
            payload = json.loads(self.job.payload)
            payload['documents'][0]['signatures'] = self.raw['document']['signatures']
            self.job.payload = json.dumps(payload)
            with self.assertRaisesRegex(UserError, 'Fresh certificate recovery validation failed'):
                self.job._preflight_submission(client)
            client.request.assert_not_called()

    def test_uncertain_submission_marker_is_preserved_and_cannot_use_certificate_proof(self):
        self.move.e_invoice_uuid = False
        self.job.write({'state': 'unknown', 'attempted_at': fields.Datetime.now(), 'attempts': 1})
        before = (self.job.state, self.job.attempted_at, self.job.attempts, self.job.payload, self.move.e_invoice_json)
        with self.routes() as (client, candidates, signer):
            with self.assertRaisesRegex(UserError, 'Only a held posted customer invoice'):
                self.job._prove_expired_certificate_recovery(client)
            candidates.assert_not_called(); signer.assert_not_called(); client.request.assert_not_called()
        self.assertEqual(before, (self.job.state, self.job.attempted_at, self.job.attempts, self.job.payload, self.move.e_invoice_json))

    def test_national_id_failure_with_valid_signature_is_not_a_certificate_retry(self):
        wrong_route = deepcopy(self.raw)
        wrong_route['validationResults'] = {'validationSteps': [
            {'name': 'Signature Validator', 'status': 'Valid'},
            {'name': 'National ID Validator', 'status': 'Invalid', 'error': {'errorCode': 'NI303'}}]}
        with self.routes() as (client, candidates, signer):
            candidates.return_value = [wrong_route]
            with self.assertRaisesRegex(UserError, 'only expired-certificate error 4159'):
                self.job._prove_expired_certificate_recovery(client)
            signer.assert_not_called(); client.request.assert_not_called()
        self.assertFalse(self.job._certificate_recovery_proof())

    def test_review_with_new_retained_signed_request_is_not_cleared_by_old_4159(self):
        recent = deepcopy(self.source)
        recent['dateTimeIssued'] = datetime.now(timezone.utc).isoformat()
        recent['signatures'] = [{'signatureType': 'I', 'value': self.signature(
            self.canonical(recent), self.new_cert, self.new_key)}]
        payload = json.dumps({'documents': [recent]})
        self.move.write({'e_invoice_json': repr(recent), 'e_invoice_canonical': self.canonical(recent),
                         'static_signature': recent['signatures'][0]['value']})
        self.job.write({'state': 'review', 'historical_review': True, 'payload': payload,
            'payload_hash': hashlib.sha256(payload.encode()).hexdigest(), 'attempted_at': fields.Datetime.now()})
        before = (self.job.state, self.job.payload, self.job.payload_hash, self.job.attempted_at,
            self.move.e_invoice_uuid, self.move.e_invoice_json, self.move.static_signature)
        with self.routes() as (client, _candidates, signer):
            with self.assertRaisesRegex(UserError, 'retained signed request differs'):
                self.job._prove_expired_certificate_recovery(client)
            signer.assert_not_called(); client.request.assert_not_called()
        self.assertFalse(self.job._certificate_recovery_proof())
        self.assertEqual(before, (self.job.state, self.job.payload, self.job.payload_hash, self.job.attempted_at,
            self.move.e_invoice_uuid, self.move.e_invoice_json, self.move.static_signature))

    def test_review_with_same_signed_bytes_but_newer_attempt_remains_uncertain(self):
        self.job.write({'state': 'review', 'historical_review': True, 'attempted_at': fields.Datetime.now()})
        before = (self.job.state, self.job.attempted_at, self.job.payload, self.move.e_invoice_json)
        with self.routes() as (client, _candidates, signer):
            with self.assertRaisesRegex(UserError, 'newer retained submission attempt'):
                self.job._prove_expired_certificate_recovery(client)
            signer.assert_not_called(); client.request.assert_not_called()
        self.assertEqual(before, (self.job.state, self.job.attempted_at, self.job.payload, self.move.e_invoice_json))
        self.assertFalse(self.job._certificate_recovery_proof())

    def test_unmatched_payload_hash_cannot_authorize_certificate_recovery(self):
        self.job.payload_hash = '0' * 64
        with self.routes() as (client, _candidates, signer):
            with self.assertRaisesRegex(UserError, 'payload hash does not match'):
                self.job._prove_expired_certificate_recovery(client)
            signer.assert_not_called(); client.request.assert_not_called()
        self.assertFalse(self.job._certificate_recovery_proof())

    def test_certificate_proof_requires_posted_customer_invoice_or_credit_note(self):
        with self.routes() as (client, candidates, signer), patch.object(type(self.move), 'move_type', 'entry'):
            with self.assertRaisesRegex(UserError, 'posted customer invoice or credit note'):
                self.job._prove_expired_certificate_recovery(client)
            candidates.assert_not_called(); signer.assert_not_called(); client.request.assert_not_called()
        self.assertFalse(self.job._certificate_recovery_proof())

    def test_wrong_identity_or_issuer_poll_cannot_consume_the_certificate_proof(self):
        with self.routes() as (client, _candidates, _signer):
            self.job._prove_expired_certificate_recovery(client)
            self.job._review_historical(client)
            self.job._prepare()
            document = json.loads(self.job.payload)['documents'][0]
            self.move.e_invoice_uuid = 'actual-new-correction'
            proof = self.job._certificate_recovery_proof()
            wrong_uuid = {'uuid': 'unrelated-authority-uuid', 'internalId': self.move.name,
                'status': 'Invalid', 'document': document, 'validationResults': {'errorCode': 'NI303'}}
            with self.assertRaises(EtaError):
                self.job._apply_remote(wrong_uuid)
            wrong_issuer = deepcopy(wrong_uuid)
            wrong_issuer['uuid'] = self.move.e_invoice_uuid
            wrong_issuer['document']['issuer']['id'] = '987654321'
            with self.assertRaisesRegex(UserError, 'issuer or invoice number'):
                self.job._apply_remote(wrong_issuer)
            self.assertEqual(self.job._certificate_recovery_proof(), proof)
            self.assertFalse(self.env['ir.attachment'].search_count([
                ('res_model', '=', 'tradeline.eta.job'), ('res_id', '=', self.job.id),
                ('name', '=like', 'ETA_certificate_recovery_outcome_%s_%%.json' % self.job.id)]))
            client.request.assert_not_called()

    def test_new_terminal_outcome_consumes_proof_without_reactivating_older_generation(self):
        with self.routes() as (client, _candidates, _signer):
            self.job._prove_expired_certificate_recovery(client)
            self.job._prove_expired_certificate_recovery(client)
            self.assertEqual(len(self.job._certificate_proof_attachments()), 2)
            saved = {a.id: a.raw for a in self.job._certificate_proof_attachments()}
            self.job._review_historical(client)
            self.job._prepare()
            approved = {'uuid': 'new-valid-certificate-correction', 'internalId': self.move.name,
                'status': 'Valid', 'dateTimeReceived': datetime.now(timezone.utc).isoformat(),
                'document': json.loads(self.job.payload)['documents'][0]}
            self.job._apply_remote(approved)
            self.assertFalse(self.job._certificate_recovery_proof())
            self.assertEqual(saved, {a.id: a.raw for a in self.job._certificate_proof_attachments()})
            self.assertEqual(self.job.state, 'valid')
            client.request.assert_not_called()

    def test_finance_corrected_new_rejection_after_expired_proof_uses_normal_gates(self):
        with self.routes() as (client, candidates, _signer):
            self.job._prove_expired_certificate_recovery(client)
            self.job._review_historical(client)
            self.job._prepare()
            attachment = self.job._certificate_proof_attachments()[:1]
            aged = json.loads(attachment.raw)
            aged['created_utc'] = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
            attachment.raw = json.dumps(aged).encode()
            retained_proof = attachment.raw
            rejected = {'uuid': 'new-id-rejection-after-certificate-correction', 'internalId': self.move.name,
                'status': 'Invalid', 'dateTimeReceived': datetime.now(timezone.utc).isoformat(),
                'document': json.loads(self.job.payload)['documents'][0],
                'validationResults': {'errorCode': 'NI303'}}
            self.job._apply_remote(rejected)
            self.assertFalse(self.job._certificate_recovery_proof())
            self.assertEqual(attachment.raw, retained_proof)
            # A genuine later Finance correction must still go through the
            # installed source/history gates, but not an expired signer proof.
            self.partner.with_context(no_vat_validation=True).vat = '12345678901234'
            self.source['receiver']['id'] = self.partner.vat
            self.move.write({'e_invoice_uuid': False, 'e_invoice_sent': False})
            self.job.write({'state': 'queued', 'payload': False, 'payload_hash': False, 'attempted_at': False})
            candidates.return_value = [rejected]
            with patch.object(type(self.job), '_validate_certificate_recovery_payload',
                              side_effect=AssertionError('A consumed certificate proof blocked a later correction')):
                self.job._prepare()
                self.assertFalse(self.job._preflight_submission(client))
                self.assertEqual(self.job.state, 'queued')
                self.job._prepare()
                self.assertTrue(self.job._preflight_submission(client))
            client.request.assert_not_called()

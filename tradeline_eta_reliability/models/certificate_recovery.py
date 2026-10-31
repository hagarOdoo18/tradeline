"""Explicit, certificate-proved recovery of unchanged 4159 rejections.

Ordinary invoices keep their existing signing path. A private, short-lived proof
is required before this module permits an unchanged expired-certificate retry.
"""
import base64
import binascii
import ast
import hashlib
import json
from datetime import datetime, timedelta, timezone

from asn1crypto import cms
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.exceptions import InvalidSignature

from odoo import fields, models, _
from odoo.exceptions import AccessError, UserError

from .historical_recovery import fiscal, fiscal_equal, leaf_codes, PreviewRollback
from .submission_safety import taxpayer


def utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError('An explicit UTC signing/issue time is required')
    return value.astimezone(timezone.utc)


def certificate_time(certificate, attribute):
    """Support both cryptography APIs; legacy certificate dates are naive UTC."""
    value = getattr(certificate, attribute + '_utc', None)
    if value is None:
        value = getattr(certificate, attribute)
        if not isinstance(value, datetime):
            raise ValueError('The certificate validity time is unreadable')
        # Older cryptography explicitly returns naive UTC, regardless of the
        # server timezone. Never interpret it as local Cairo/system time.
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
    return utc(value)


def signer_certificate(signature, canonical=None, valid_at=()):
    """Resolve the actual CMS signer; verify its signature, digest and times.

    This proves the selected certificate signed these bytes. ETA remains the
    authority for taxpayer entitlement, trusted roots and revocation status.
    """
    try:
        content = cms.ContentInfo.load(base64.b64decode(''.join(signature.split()), validate=True), strict=True)
        if content['content_type'].native != 'signed_data':
            raise ValueError('Expected CMS SignedData')
        data = content['content']; signers = data['signer_infos']
        if len(signers) != 1 or signers[0]['digest_algorithm']['algorithm'].native != 'sha256':
            raise ValueError('Expected one SHA256 signer')
        signer = signers[0]; sid = signer['sid']; matching = []
        for choice in data['certificates']:
            if choice.name != 'certificate':
                continue
            cert = choice.chosen
            if (sid.name == 'issuer_and_serial_number'
                    and cert.issuer.dump() == sid.chosen['issuer'].dump()
                    and cert.serial_number == sid.chosen['serial_number'].native
                    or sid.name == 'subject_key_identifier' and cert.key_identifier == sid.native):
                matching.append(cert)
        if len(matching) != 1:
            raise ValueError('The actual signing certificate is missing or ambiguous')
        selected = matching[0]; certificate = x509.load_der_x509_certificate(selected.dump())
        attributes = signer['signed_attrs']
        digests = [a['values'][0].native for a in attributes if a['type'].native == 'message_digest']
        if len(digests) != 1 or canonical is not None and digests != [hashlib.sha256(canonical.encode('utf-8')).digest()]:
            raise ValueError('The fresh signature does not match the canonical document')
        if signer['signature_algorithm']['algorithm'].native not in ('rsassa_pkcs1v15', 'sha256_rsa'):
            raise ValueError('Unsupported signing-certificate proof algorithm')
        public_key = certificate.public_key()
        if not isinstance(public_key, rsa.RSAPublicKey):
            raise ValueError('Expected the configured RSA signing certificate')
        # CMS signs the universal SET encoding, rather than its implicit [0]
        # wrapper inside SignerInfo. Do not verify with an unrelated CA key.
        public_key.verify(signer['signature'].native, attributes.untag().dump(), padding.PKCS1v15(), hashes.SHA256())
        times = [a['values'][0].native for a in attributes if a['type'].native == 'signing_time']
        if len(times) > 1:
            raise ValueError('Ambiguous signing time')
        signing_time = utc(times[0]) if times else None
        before = certificate_time(certificate, 'not_valid_before')
        after = certificate_time(certificate, 'not_valid_after')
        checks = [utc(t) for t in valid_at]
        if checks and signing_time:
            checks.append(signing_time)
        if any(t < before or t > after for t in checks):
            raise ValueError('The actual signing certificate is expired or not yet valid')
        return {'fingerprint': hashlib.sha256(selected.dump()).hexdigest(),
                'not_before': before.isoformat(), 'not_after': after.isoformat(),
                'signing_time': signing_time.isoformat() if signing_time else None}
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, binascii.Error, InvalidSignature) as exc:
        raise ValueError('Signing certificate proof failed: %s' % (str(exc) or type(exc).__name__)) from exc


def document_signature(document):
    signatures = document.get('signatures') if isinstance(document, dict) else None
    if (not isinstance(signatures, list) or len(signatures) != 1
            or signatures[0].get('signatureType') != 'I' or not signatures[0].get('value')):
        raise ValueError('The document must retain one issuer signature')
    return signatures[0]['value']


class EtaCertificateRecovery(models.Model):
    _inherit = 'tradeline.eta.job'

    def _certificate_configuration_hash(self):
        company = self.company_id.sudo()
        # Store a scope hash, never signer credentials in the proof or logs.
        values = [company.id, company.config_type, taxpayer(company.vat), company.client_id, company.client_secret,
                  company.signature_url, company.signature_label, company.signature_pin]
        return hashlib.sha256(json.dumps(values, default=str).encode()).hexdigest()

    def _certificate_source_hash(self, source):
        return hashlib.sha256(json.dumps(fiscal(source), sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _certificate_proof_attachments(self):
        return self.env['ir.attachment'].sudo().search([
            ('res_model', '=', self._name), ('res_id', '=', self.id), ('company_id', '=', self.company_id.id),
            ('name', '=like', 'ETA_certificate_recovery_proof_%s_%%.json' % self.id)], order='id desc')

    def _certificate_recovery_proof(self):
        self.ensure_one()
        # A renewed proof supersedes every older generation. Consuming the
        # newest proof must not accidentally reactivate an older one.
        attachment = self._certificate_proof_attachments()[:1]
        if not attachment:
            return None
        try:
            outcomes = self.env['ir.attachment'].sudo().search([
                ('res_model', '=', self._name), ('res_id', '=', self.id), ('company_id', '=', self.company_id.id),
                ('name', '=', 'ETA_certificate_recovery_outcome_%s_%s.json' % (self.id, attachment.id))])
            for marker in outcomes:
                outcome = json.loads(marker.raw)
                if (outcome['database'] != self.env.cr.dbname or outcome['job'] != self.id
                        or outcome['company'] != self.company_id.id or outcome['proof_attachment'] != attachment.id):
                    raise ValueError('Certificate outcome belongs to another scope')
            return None if outcomes else json.loads(attachment.raw)
        except (ValueError, TypeError, KeyError):
            raise UserError(_('The retained certificate recovery proof is unreadable. IT must review it; nothing was sent.'))

    def _apply_remote(self, data):
        proof = self._certificate_recovery_proof()
        result = super()._apply_remote(data)
        # Only the inherited identity/issuer/UUID-checked authority result can
        # retire this one-use signing proof. Keep the old proof immutable and
        # available to the same-certificate rejection guard.
        originals = {r.get('uuid') for r in proof.get('original_receipts', [])} if proof else set()
        if (proof and data.get('status') in ('Valid', 'Invalid', 'Rejected', 'Cancelled')
                and data.get('uuid') and data['uuid'] not in originals):
            attachments = self._certificate_proof_attachments()
            attachment = next((a for a in attachments if json.loads(a.raw) == proof), None)
            if not attachment:
                raise UserError(_('The certificate proof could not be matched to its retained evidence. IT must review it.'))
            self.env['ir.attachment'].sudo().create({
                'name': 'ETA_certificate_recovery_outcome_%s_%s.json' % (self.id, attachment.id),
                'res_model': self._name, 'res_id': self.id, 'company_id': self.company_id.id,
                'mimetype': 'application/json', 'public': False,
                'raw': json.dumps({'database': self.env.cr.dbname, 'job': self.id, 'company': self.company_id.id,
                    'proof_attachment': attachment.id, 'recorded_utc': datetime.now(timezone.utc).isoformat(),
                    'authority_uuid': data['uuid'], 'authority_status': data['status'],
                    'authority_internal_id': data['internalId'], 'payload_hash': self.payload_hash,
                    'reason': 'Identity-matched new terminal authority receipt consumed this signing proof.'}).encode()})
        return result

    def _assert_certificate_attempt_reconciled(self, receipts):
        """Do not hide a newer uncertain request behind an older Invalid copy."""
        move = self.move_id
        if self.duplicate_retry_at or self.duplicate_payload_hash:
            raise UserError(_('Reconcile the retained duplicate/uncertain submission marker before certificate recovery.'))
        documents = {}
        for receipt in receipts:
            document = receipt.get('document')
            document = json.loads(document) if isinstance(document, str) else document
            documents[receipt['uuid']] = (document, receipt)
        known = {uuid for uuid in (move.e_invoice_uuid, move.l10n_eg_uuid) if uuid}
        if not known.issubset(documents) or move.e_invoice_sent and not known:
            raise UserError(_('A retained sent/UUID marker is not reconciled to the verified Invalid receipts. Nothing was changed.'))
        matches = set(documents)
        retained = []
        try:
            if self.payload_hash and not self.payload:
                raise ValueError('The retained payload hash has no identifiable request bytes')
            if self.payload:
                digest = hashlib.sha256(self.payload.encode()).hexdigest()
                if self.payload_hash and self.payload_hash != digest:
                    raise ValueError('The retained signed payload hash does not match its bytes')
                payload = json.loads(self.payload)
                if not isinstance(payload.get('documents'), list) or len(payload['documents']) != 1:
                    raise ValueError('The retained signed request is not one verified document')
                retained.append(payload['documents'][0])
            if move.e_invoice_json:
                try:
                    saved = json.loads(move.e_invoice_json)
                except (ValueError, TypeError):
                    saved = ast.literal_eval(move.e_invoice_json)
                if isinstance(saved, bytes):
                    try:
                        saved = json.loads(saved.decode())
                    except ValueError:
                        saved = ast.literal_eval(saved.decode())
                if not isinstance(saved, dict):
                    raise ValueError('The retained invoice document is unreadable')
                if saved.get('signatures') or move.invoice_signed or move.e_invoice_sent:
                    retained.append(saved)
            for document in retained:
                # Full signed-document equality deliberately retains the issue
                # time and signature; fiscal-only equality cannot prove which
                # HTTP attempt an old rejection belongs to.
                exact = json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
                matches &= {uuid for uuid, (candidate, _receipt) in documents.items()
                    if json.dumps(candidate, sort_keys=True, ensure_ascii=False, separators=(',', ':')) == exact}
            if retained and not matches:
                raise ValueError('A retained signed request differs from every verified Invalid document')
            if move.e_invoice_uuid and retained and move.e_invoice_uuid not in matches:
                raise ValueError('The retained request and recorded ETA UUID identify different attempts')
            if move.static_signature:
                matches &= {uuid for uuid in matches
                    if document_signature(documents[uuid][0]) == move.static_signature}
                if not matches:
                    raise ValueError('The retained signature is not the one rejected in the verified receipts')
            if (self.attempted_at or move.invoice_signed) and not retained:
                raise ValueError('A retained attempted/signed request cannot be identified')
            correlated = matches & known if known else matches
            if self.attempted_at or move.e_invoice_date:
                received = [utc(documents[uuid][1]['dateTimeReceived']) for uuid in correlated]
                if not received:
                    raise ValueError('The retained attempt has no identity-matched Invalid receipt')
                latest = max(received)
                for marker in (self.attempted_at, move.e_invoice_date):
                    if marker and (utc(marker) if marker.tzinfo else marker.replace(tzinfo=timezone.utc)) > latest:
                        raise ValueError('A newer retained submission attempt is not covered by the older Invalid receipt')
        except (ValueError, TypeError, KeyError, IndexError, SyntaxError) as exc:
            raise UserError(_('Reconcile the outstanding signed/uncertain request before certificate recovery (%s). Nothing was changed.', str(exc))) from exc

    def _check_certificate_recovery_proof(self, proof, source):
        now = datetime.now(timezone.utc)
        try:
            age = now - utc(proof['created_utc'])
            valid = (proof['database'] == self.env.cr.dbname and proof['job'] == self.id
                and proof['company'] == self.company_id.id
                and proof['configuration_hash'] == self._certificate_configuration_hash()
                and proof['financial_fingerprint'] == self._historical_financial_fingerprint()
                and proof['source_hash'] == self._certificate_source_hash(source)
                and timedelta(0) <= age < timedelta(minutes=15))
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            raise UserError(_('The certificate recovery proof expired or its source/configuration changed. Recheck the complete authority history and fixed signer; do not resend it manually.'))

    def _certificate_retry_allowed(self, receipts, source):
        if (not receipts or any(r.get('status') != 'Invalid'
                or set(leaf_codes(r.get('validationResults', {}))) != {'4159'} for r in receipts)):
            return False
        proof = self._certificate_recovery_proof()
        if not proof:
            return False
        self._check_certificate_recovery_proof(proof, source)
        return proof['receipt_set_hash'] == self._receipt_set_hash(receipts)

    def _validate_certificate_recovery_payload(self, proof):
        try:
            document = json.loads(self.payload)['documents'][0]
            issued = utc(document['dateTimeIssued']); now = datetime.now(timezone.utc)
            self._check_certificate_recovery_proof(proof, document)
            if (self.issue_date_policy != 'current' or not self.issue_date_approved_by
                    or not self.issue_date_approved_at or not self.issue_date_approval_note
                    or issued < utc(proof['created_utc']) - timedelta(seconds=5)
                    or issued > now + timedelta(minutes=5)):
                raise ValueError('A fresh, approved current ETA issue time is required')
            certificate = signer_certificate(document_signature(document), self.move_id.e_invoice_canonical,
                                             valid_at=(issued, now))
            if (certificate['fingerprint'] != proof['fresh_certificate']['fingerprint']
                    or certificate['fingerprint'] in proof['expired_certificate_fingerprints']):
                raise ValueError('The verified replacement certificate changed or an expired original was reused')
            signing = certificate['signing_time']
            if signing and (utc(signing) < utc(proof['created_utc']) - timedelta(seconds=5)
                            or utc(signing) > now + timedelta(minutes=5)):
                raise ValueError('The signer returned an old or future signing time')
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            raise UserError(_('Fresh certificate recovery validation failed (%s). IT must fix the signer; nothing was submitted.', str(exc))) from exc

    def _prepare(self):
        proof = self._certificate_recovery_proof()
        if proof:
            self._check_certificate_recovery_proof(proof, self._submission_source())
        result = super()._prepare()
        if proof:
            self._validate_certificate_recovery_payload(proof)
        return result

    def _preflight_submission(self, client):
        proof = self._certificate_recovery_proof()
        result = super()._preflight_submission(client)
        if result and proof:
            # The inherited guard freshly checks every authority receipt. Its
            # exact rejected set must still be the one this proof authorized.
            if self.authority_receipt_hash != proof['receipt_set_hash']:
                raise UserError(_('The authority receipt set changed after certificate proof. Reconcile it before any submission.'))
            self._validate_certificate_recovery_payload(proof)
        return result

    def _prove_expired_certificate_recovery(self, client):
        """Private explicit admin operation: GET-only proof, then normal review.

        The caller commits each bounded job and yields the shared transport lock.
        No ETA document is posted here and no source writes survive the preview.
        """
        self.ensure_one()
        if not (self.env.is_superuser() or self.env.user.has_group('base.group_system')
                or self.env.user.has_group('account.group_account_manager')):
            raise AccessError(_('An accounting manager must verify certificate-only recovery.'))
        params = self.env['ir.config_parameter'].sudo()
        for key in ('enabled_database', 'historical_recovery_database'):
            if params.get_param('tradeline_eta_reliability.' + key) != self.env.cr.dbname:
                raise UserError(_('Certificate recovery is not enabled for this database.'))
        self.env.cr.execute('SELECT pg_try_advisory_xact_lock(741181,1)')
        if not self.env.cr.fetchone()[0]:
            raise UserError(_('The ETA transport is busy. Retry this proof later.'))
        self.env.cr.execute('SELECT pg_try_advisory_xact_lock(741180,%s)', [self.id])
        if not self.env.cr.fetchone()[0]:
            raise UserError(_('This invoice is being processed. Refresh it before proving recovery.'))
        move = self.move_id
        self.env.cr.execute('SELECT id FROM account_move WHERE id=%s FOR UPDATE NOWAIT', [move.id])
        self.invalidate_recordset(); move.invalidate_recordset()
        if (self.state not in ('invalid', 'blocked', 'review') or move.state != 'posted'
                or move.move_type not in ('out_invoice', 'out_refund')):
            raise UserError(_('Only a held posted customer invoice or credit note with verified expired-certificate rejection can use this proof.'))
        self._assert_eta_number_unique()
        before = self._transmission_fingerprint(); financial = self._historical_financial_fingerprint()
        source = self._submission_source(); history = self._history_snapshot()
        if not history or datetime.combine(move.invoice_date, datetime.min.time()) < history['from']:
            raise UserError(_('Complete taxpayer-scoped authority history is required before certificate recovery.'))
        receipts = self._history_candidates(client, history)
        if (not receipts or any(r.get('status') != 'Invalid'
                or set(leaf_codes(r.get('validationResults', {}))) != {'4159'} for r in receipts)):
            raise UserError(_('Certificate recovery requires exclusively Invalid receipts with only expired-certificate error 4159. Reconcile approvals, pending or other errors first.'))
        self._assert_certificate_attempt_reconciled(receipts)
        expired = set(); now = datetime.now(timezone.utc)
        try:
            for receipt in receipts:
                document = receipt.get('document')
                document = json.loads(document) if isinstance(document, str) else document
                self._assert_eta_document_issuer(document)
                if not fiscal_equal(fiscal(document), fiscal(source)):
                    raise ValueError('The expired-signature receipt differs from the current fiscal source')
                if utc(document['dateTimeIssued']).replace(tzinfo=None) < history['from']:
                    raise ValueError('The retained receipt predates the complete authority history')
                old = signer_certificate(document_signature(document))
                if utc(old['not_after']) >= now:
                    raise ValueError('The authority error and selected original certificate expiry need IT reconciliation')
                expired.add(old['fingerprint'])
            proof_started = datetime.now(timezone.utc)
            preview = None
            try:
                with self.env.cr.savepoint():
                    self.write({'issue_date_policy': 'current', 'current_issue_datetime': False,
                        'issue_date_approved_by': self.env.uid, 'issue_date_approved_at': fields.Datetime.now(),
                        'issue_date_approval_note': 'Owner-approved current ETA issue-date recovery, preserving original accounting dates and amounts. Certificate-only signing preview; no submission.'})
                    # Bypass only our proof-dependent wrapper, not the installed
                    # serializer, signer, digest, quantities or date preparation.
                    super(EtaCertificateRecovery, self)._prepare()
                    preview = json.loads(self.payload)['documents'][0]
                    issued = utc(preview['dateTimeIssued'])
                    if (not fiscal_equal(fiscal(preview), fiscal(source))
                            or not proof_started - timedelta(seconds=5) <= issued <= datetime.now(timezone.utc) + timedelta(minutes=5)):
                        raise ValueError('The fresh signing preview changed the fiscal source or issue time')
                    fresh = signer_certificate(document_signature(preview), move.e_invoice_canonical,
                                               valid_at=(issued, datetime.now(timezone.utc)))
                    if fresh['fingerprint'] in expired:
                        raise ValueError('The signing service reused an expired original certificate')
                    if (fresh['signing_time'] and not proof_started - timedelta(seconds=5)
                            <= utc(fresh['signing_time']) <= datetime.now(timezone.utc) + timedelta(minutes=5)):
                        raise ValueError('The signing service returned an old or future signing time')
                    raise PreviewRollback()
            except PreviewRollback:
                pass
            finally:
                self.invalidate_recordset(); move.invalidate_recordset()
            if before != self._transmission_fingerprint() or financial != self._historical_financial_fingerprint():
                raise ValueError('The source changed during rollback-only certificate preview')
            for attachment in self._certificate_proof_attachments():
                previous = json.loads(attachment.raw)
                if previous.get('fresh_certificate', {}).get('fingerprint') == fresh['fingerprint']:
                    for receipt in receipts:
                        if utc(receipt['dateTimeReceived']) >= utc(previous['created_utc']):
                            raise ValueError('ETA already rejected this replacement certificate after its previous proof; IT must fix the signer')
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            raise UserError(_('Expired-certificate recovery proof failed (%s). Nothing was submitted.', str(exc))) from exc
        proof = {'database': self.env.cr.dbname, 'job': self.id, 'company': self.company_id.id,
            'created_utc': proof_started.isoformat(), 'configuration_hash': self._certificate_configuration_hash(),
            'receipt_set_hash': self._receipt_set_hash(receipts), 'financial_fingerprint': financial,
            'source_hash': self._certificate_source_hash(source), 'fresh_certificate': fresh,
            'expired_certificate_fingerprints': sorted(expired), 'original_receipts': receipts,
            'preview_document': preview, 'preview_only': True, 'document_posts': 0}
        self.env['ir.attachment'].sudo().create({'name': 'ETA_certificate_recovery_proof_%s_%s.json' % (self.id, fresh['fingerprint'][:16]),
            'res_model': self._name, 'res_id': self.id, 'company_id': self.company_id.id,
            'mimetype': 'application/json', 'public': False, 'raw': json.dumps(proof, ensure_ascii=False).encode()})
        self.write({'state': 'review', 'historical_review': True, 'historical_submission_verified': False, 'next_attempt': False})
        return True

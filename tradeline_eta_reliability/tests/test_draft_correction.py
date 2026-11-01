import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

from lxml import etree
from contextlib import ExitStack

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user


@tagged('post_install', '-at_install')
class TestEtaDraftCorrection(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.original_company = cls.env.company
        cls.env.ref('base.EGP').active = True
        partner_values = {'name': 'VAT correction QA company', 'is_company': True,
            'vat': '123456789', 'country_id': cls.env.ref('base.eg').id}
        for name, value in [('company_size', 'small'), ('company_device', 1)]:
            if name in cls.env['res.partner']._fields:
                partner_values[name] = value
        company_partner = cls.env['res.partner'].with_context(no_vat_validation=True).create(partner_values)
        cls.company = cls.env['res.company'].create({'name': 'VAT correction QA company',
            'partner_id': company_partner.id,
            'currency_id': cls.env.ref('base.EGP').id, 'country_id': cls.env.ref('base.eg').id,
            'activity_code_id': cls.env.company.activity_code_id.id,
            'type_code_id': cls.env.company.type_code_id.id, 'sub_type_code_id': cls.env.company.sub_type_code_id.id})
        cls.env = cls.env(context=dict(cls.env.context, allowed_company_ids=cls.company.ids))
        values = {'vat': '123456789'}
        for name, value in [('company_size', 'small'), ('company_device', 1)]:
            if name in cls.env['res.partner']._fields:
                values[name] = value
        cls.company.partner_id.with_context(no_vat_validation=True).write(values)
        cls.company.write({'client_id': 'draft-test', 'client_secret': 'draft-secret', 'config_type': 'uat'})
        cls.receivable = cls.env['account.account'].create({'name': 'Draft correction receivable', 'code': 'DRAFTAR',
            'account_type': 'asset_receivable', 'reconcile': True, 'company_ids': [Command.set(cls.company.ids)]})
        cls.income = cls.env['account.account'].create({'name': 'Draft correction income', 'code': 'DRAFTINC',
            'account_type': 'income', 'company_ids': [Command.set(cls.company.ids)]})
        cls.partner = cls.env['res.partner'].create({'name': 'Draft correction QA buyer',
            'property_account_receivable_id': cls.receivable.id})
        cls.journal = cls.env['account.journal'].create({'name': 'Draft correction QA', 'code': 'DRFT',
            'type': 'sale', 'company_id': cls.company.id, 'edi_format_ids': [Command.clear()]})
        cls.eta_format = cls.env.ref('l10n_eg_edi_eta.edi_eg_eta')
        cls.product = cls.env['product.product'].create({'name': 'VAT correction product'})
        cls.tax_group = cls.env['account.tax.group'].create({'name': 'VAT correction QA taxes',
            'company_id': cls.company.id, 'country_id': cls.env.ref('base.eg').id})
        vat_type = cls.env['account.tax.type.code'].search([('code', '=', 'T1')], limit=1)
        vat_type = vat_type or cls.env['account.tax.type.code'].create({'code': 'T1', 'ar_name': 'VAT'})
        subtype = cls.env['account.tax.sub.type.code'].search([('code', '=', 'V009')], limit=1)
        subtype = subtype or cls.env['account.tax.sub.type.code'].create({'code': 'V009', 'ar_name': 'VAT', 'type_id': vat_type.id})
        cls.vat = cls.env['account.tax'].create({'name': 'VAT correction 14%', 'amount': 14,
            'type_tax_use': 'sale', 'type_code_id': vat_type.id, 'sub_type_code_id': subtype.id,
            'price_include_override': 'tax_included', 'tax_group_id': cls.tax_group.id})
        cls.tax_account = cls.env['account.account'].create({'name': 'VAT correction payable', 'code': 'DRAFTVAT',
            'account_type': 'liability_current', 'company_ids': [Command.set(cls.company.ids)]})
        (cls.vat.invoice_repartition_line_ids | cls.vat.refund_repartition_line_ids).filtered(
            lambda line: line.repartition_type == 'tax').account_id = cls.tax_account
        cls.vat_base_tag = cls.env['account.account.tag'].create({'name': 'VAT correction QA base',
            'applicability': 'taxes', 'country_id': cls.env.ref('base.eg').id})
        cls.vat_tax_tag = cls.env['account.account.tag'].create({'name': 'VAT correction QA tax',
            'applicability': 'taxes', 'country_id': cls.env.ref('base.eg').id})
        for line in cls.vat.invoice_repartition_line_ids:
            line.tag_ids = cls.vat_base_tag if line.repartition_type == 'base' else cls.vat_tax_tag
        cls.general = cls.env['account.journal'].create({'name': 'VAT correction adjustments', 'code': 'VATADJ',
            'type': 'general', 'company_id': cls.company.id})

    def setUp(self):
        super().setUp()
        guard = patch('requests.sessions.Session.request', side_effect=AssertionError('Unexpected external HTTP'))
        guard.start()
        self.addCleanup(guard.stop)
        if 'e_invoicing_code' not in self.product._fields:
            metadata = patch.object(type(self.product), 'e_invoicing_code', 'VAT-QA-ITEM', create=True)
            metadata.start()
            self.addCleanup(metadata.stop)
        else:
            self.product.e_invoicing_code = 'VAT-QA-ITEM'

    def invoice(self):
        move = self.env['account.move'].create({'move_type': 'out_invoice', 'journal_id': self.journal.id,
            'partner_id': self.partner.id, 'invoice_date': fields.Date.today(),
            'invoice_line_ids': [Command.create({'name': 'Draft correction item', 'account_id': self.income.id,
                'quantity': 1, 'price_unit': 100, 'tax_ids': [Command.clear()]})]})
        move.action_post()
        return move

    def edi(self, move, state):
        return self.env['account.edi.document'].create({
            'move_id': move.id, 'edi_format_id': self.eta_format.id, 'state': state})

    def test_unsent_reset_add_tax_repost_reuses_clean_job(self):
        move = self.invoice()
        job = move.eta_job_ids
        job.write({'payload': '{"old": "tax-free"}', 'payload_hash': 'old-hash',
                   'historical_fingerprint': 'old-proof', 'historical_submission_verified': True})
        move.write({'invoice_signed': True, 'e_invoice_json': 'old-signed', 'eta_submission_pending': True})
        move.button_draft()
        self.assertEqual(move.state, 'draft')
        self.assertFalse(move.eta_submission_pending)
        self.assertFalse(move.invoice_signed)
        self.assertFalse(job.payload)
        self.assertFalse(job.payload_hash)
        self.assertFalse(job.historical_fingerprint)
        self.assertFalse(job.historical_submission_verified)
        archive = self.env['ir.attachment'].search([
            ('res_model', '=', 'account.move'), ('res_id', '=', move.id),
            ('name', '=', 'ETA_before_draft_%s.json' % job.id)])
        self.assertEqual(json.loads(archive.raw)['payload_hash'], 'old-hash')
        tax = self.env['account.tax'].create({'name': 'Draft correction 14%', 'amount': 14,
            'amount_type': 'percent', 'type_tax_use': 'sale', 'company_id': self.company.id})
        move.invoice_line_ids.tax_ids = tax
        move.action_post()
        self.assertEqual(move.amount_tax, 14)
        self.assertEqual(move.amount_total, 114)
        self.assertEqual(move.eta_job_ids, job)
        self.assertEqual(job.state, 'queued')
        self.assertFalse(job.payload)
        move.button_draft()  # A second correction remains available.
        self.assertEqual(move.state, 'draft')

    def test_stale_native_cancel_flag_does_not_hide_unsent_reset(self):
        move = self.invoice()
        doc = self.edi(move, 'to_cancel')
        self.assertTrue(move.show_reset_to_draft_button)
        move.button_draft()
        self.assertEqual(move.state, 'draft')
        self.assertFalse(doc.exists())

    def test_stale_native_sent_flag_does_not_block_unsent_reset(self):
        move = self.invoice()
        self.edi(move, 'sent')
        self.assertTrue(move._edi_allow_button_draft())
        self.assertTrue(move.show_reset_to_draft_button)
        move.button_draft()
        self.assertEqual(move.state, 'draft')

    def test_accepted_job_blocks_reset_even_when_invoice_receipt_was_erased(self):
        for state in ('accepted', 'valid'):
            with self.subTest(state=state):
                move = self.invoice()
                move.eta_job_ids.state = state
                self.assertFalse(move.show_reset_to_draft_button)
                with self.assertRaisesRegex(UserError, 'receipt|unresolved'):
                    move.button_draft()
                self.assertEqual(move.state, 'posted')
                self.assertEqual(move.eta_job_ids.state, state)

    def test_known_receipt_is_not_erased(self):
        move = self.invoice()
        move.write({'e_invoice_uuid': 'known-receipt', 'e_invoice_status': 'Valid', 'e_invoice_sent': True})
        with self.assertRaisesRegex(UserError, 'receipt|unresolved'):
            move.button_draft()
        self.assertEqual(move.e_invoice_uuid, 'known-receipt')
        self.assertEqual(move.e_invoice_status, 'Valid')
        self.assertFalse(move.show_reset_to_draft_button)

    def test_native_receipt_is_not_cleared_before_guard(self):
        move = self.invoice()
        attachment = self.env['ir.attachment'].create({'name': 'Draft QA ETA receipt.json',
            'res_model': 'account.move', 'res_id': move.id, 'mimetype': 'application/json',
            'raw': json.dumps({'response': {'l10n_eg_uuid': 'native-known'}}).encode()})
        move.l10n_eg_eta_json_doc_id = attachment
        self.assertFalse(move.show_reset_to_draft_button)
        with self.assertRaisesRegex(UserError, 'receipt|unresolved'):
            move.button_draft()
        self.assertEqual(move.l10n_eg_eta_json_doc_id, attachment)
        self.assertEqual(move.l10n_eg_uuid, 'native-known')

    def test_uncertain_and_duplicate_attempts_preserve_evidence(self):
        cases = [{'state': 'unknown'}, {'attempted_at': fields.Datetime.now()}, {'attempts': 1},
                 {'duplicate_payload_hash': 'uncertain-copy'},
                 {'duplicate_retry_at': fields.Datetime.now() + timedelta(minutes=5)},
                 {'state': 'review', 'historical_review': True}, {'submission_uuid': 'known-submission'}]
        for values in cases:
            with self.subTest(values=values):
                move = self.invoice()
                job = move.eta_job_ids
                job.write(dict({'payload': 'preserved-request'}, **values))
                self.assertFalse(move.show_reset_to_draft_button)
                with self.assertRaisesRegex(UserError, 'receipt|unresolved'):
                    move.button_draft()
                self.assertEqual(job.payload, 'preserved-request')
                self.assertEqual(move.state, 'posted')

    def test_explicitly_rejected_unsent_request_can_be_corrected(self):
        move = self.invoice()
        job = move.eta_job_ids
        job.write({'state': 'blocked', 'attempted_at': fields.Datetime.now(), 'attempts': 1,
                   'safe_to_retry': True, 'payload': 'rejected-request'})
        self.assertTrue(move.show_reset_to_draft_button)
        move.button_draft()
        self.assertEqual(job.state, 'queued')
        self.assertEqual(job.attempts, 0)
        self.assertFalse(job.safe_to_retry)
        move.action_post()
        self.assertTrue(move.show_reset_to_draft_button)

    def test_mixed_selection_is_checked_before_any_receipt_or_payload_is_cleared(self):
        safe, accepted = self.invoice(), self.invoice()
        safe.eta_job_ids.payload = 'preserve-on-failure'
        accepted.write({'e_invoice_uuid': 'accepted-receipt', 'e_invoice_status': 'Valid'})
        with self.assertRaisesRegex(UserError, 'receipt|unresolved'):
            (safe | accepted).button_draft()
        self.assertEqual(safe.state, 'posted')
        self.assertEqual(safe.eta_job_ids.payload, 'preserve-on-failure')
        self.assertEqual(accepted.e_invoice_uuid, 'accepted-receipt')

    def test_other_edi_formats_keep_cancellation_protection(self):
        move = self.invoice()
        other = self.env['account.edi.format'].create({'name': 'Draft QA other EDI', 'code': 'draft_qa'})
        self.env['account.edi.document'].create({'move_id': move.id, 'edi_format_id': other.id, 'state': 'sent'})
        with patch.object(type(other), '_needs_web_services', return_value=True), \
                patch.object(type(other), '_get_move_applicability', return_value={'cancel': lambda m: {}}):
            self.assertFalse(move.show_reset_to_draft_button)
            with self.assertRaises(UserError):
                move.button_draft()

    def test_locked_invoice_remains_not_draftable(self):
        move = self.invoice()
        move.inalterable_hash = 'QA-locked-entry'
        self.assertFalse(move.show_reset_to_draft_button)
        with self.assertRaises(UserError):
            move.button_draft()
        self.assertEqual(move.state, 'posted')

    def test_invoice_lines_are_editable_only_in_draft(self):
        view = self.env['account.move'].get_view(view_type='form')
        root = etree.fromstring(view['arch'].encode())
        self.assertEqual(root.xpath("//field[@name='invoice_line_ids']")[0].get('readonly'), "state != 'draft'")

    def test_active_transport_lock_blocks_reset_without_losing_payload(self):
        move = self.invoice()
        job = move.eta_job_ids
        job.payload = 'worker-owned-payload'
        with self.env.registry.cursor() as other:
            other.execute('SELECT pg_advisory_xact_lock(741180, %s)', [job.id])
            with self.assertRaisesRegex(UserError, 'being processed'):
                move.button_draft()
            self.assertEqual(move.state, 'posted')
            self.assertEqual(job.payload, 'worker-owned-payload')

    def test_legacy_signed_document_without_job_requires_reconciliation(self):
        self.company.client_secret = False
        move = self.invoice()
        self.assertFalse(move.eta_job_ids)
        self.company.client_secret = 'draft-secret'
        move.e_invoice_json = 'legacy-signed-payload'
        self.assertFalse(move.show_reset_to_draft_button)
        with self.assertRaisesRegex(UserError, 'receipt|unresolved'):
            move.button_draft()
        self.assertEqual(move.e_invoice_json, 'legacy-signed-payload')

    def vat_invoice(self):
        move = self.env['account.move'].create({'move_type': 'out_invoice', 'journal_id': self.journal.id,
            'partner_id': self.partner.id, 'invoice_date': fields.Date.today(),
            'currency_id': self.env.ref('base.EGP').id,
            'invoice_line_ids': [Command.create({'product_id': self.product.id, 'account_id': self.income.id,
                'quantity': 1, 'price_unit': 100, 'tax_ids': [Command.clear()]})]})
        move.action_post()
        return move

    def vat_wizard(self, move):
        return self.env['tradeline.eta.vat.correction'].create({'move_id': move.id,
            'tax_id': self.vat.id, 'adjustment_journal_id': self.general.id})

    def receipt(self, move, status='Valid'):
        return {'uuid': 'VAT-ORIGINAL-RECEIPT', 'internalId': move.name, 'status': status,
            'dateTimeReceived': '2026-11-01T01:00:00Z', 'submissionUUID': 'VAT-QA-SUBMISSION',
            'document': {'internalID': move.name, 'issuer': {'id': '123456789'},
                'totalAmount': 100, 'netAmount': 100, 'taxTotals': [{'taxType': 'T1', 'amount': 0}],
                'invoiceLines': [{'itemCode': 'VAT-QA-ITEM', 'quantity': 1, 'netTotal': 100,
                    'discount': {'rate': 0, 'amount': 0}, 'taxableItems': []}]}}

    def test_correct_vat_unsent_opens_same_draft_with_vat_without_http(self):
        move = self.vat_invoice()
        action = self.vat_wizard(move).action_prepare()
        self.assertEqual(action['res_id'], move.id)
        self.assertEqual(move.state, 'draft')
        self.assertAlmostEqual(move.amount_tax, 12.28, places=2)
        self.assertFalse(move.eta_vat_correction_ids)

    def test_valid_invoice_prepares_linked_credit_and_vat_replacement_without_post(self):
        move = self.vat_invoice()
        receipt = self.receipt(move)
        move.write({'e_invoice_uuid': receipt['uuid'], 'e_invoice_status': 'Valid', 'e_invoice_sent': True})
        move.eta_job_ids.state = 'valid'
        wizard = self.vat_wizard(move)
        with patch('odoo.addons.tradeline_eta_reliability.models.draft_correction.EtaClient.request', return_value=receipt) as request:
            wizard.action_prepare()
            request.assert_called_once_with('GET', '/api/v1.0/documents/%s/raw' % receipt['uuid'])
        corrections = move.eta_vat_correction_ids
        self.assertEqual(len(corrections), 2)
        self.assertTrue(all(record.state == 'draft' for record in corrections))
        credit = corrections.filtered(lambda record: record.move_type == 'out_refund')
        replacement = corrections.filtered(lambda record: record.move_type == 'out_invoice')
        self.assertEqual(credit.amount_total, 100)
        self.assertEqual(credit.amount_tax, 0)
        self.assertEqual(credit.e_invoice_credit_src, receipt['uuid'])
        self.assertEqual(replacement.amount_total, 100)
        self.assertAlmostEqual(replacement.amount_tax, 12.28, places=2)
        self.assertEqual(move.state, 'posted')
        self.assertEqual(move.e_invoice_uuid, receipt['uuid'])
        self.assertFalse(corrections.eta_job_ids)
        wizard.action_prepare()  # A second click reopens these drafts.
        self.assertEqual(len(move.eta_vat_correction_ids), 2)

    def test_already_edited_invoice_gets_balanced_draft_alignment(self):
        move = self.vat_invoice()
        receipt = self.receipt(move)
        move.button_draft()
        move.invoice_line_ids.tax_ids = self.vat
        move.action_post()
        move.eta_job_ids.state = 'valid'
        saved = self.env['ir.attachment'].create({'name': 'ETA_old.json', 'res_model': 'account.move',
            'res_id': move.id, 'mimetype': 'application/json', 'raw': json.dumps({
                'request': receipt['document'], 'response': {'l10n_eg_uuid': receipt['uuid']}}).encode()})
        self.assertFalse(move.e_invoice_uuid)
        with patch('odoo.addons.tradeline_eta_reliability.models.draft_correction.EtaClient.request', return_value=receipt):
            self.vat_wizard(move).action_prepare()
        corrections = move.eta_vat_correction_ids
        credit = corrections.filtered(lambda record: record.move_type == 'out_refund')
        replacement = corrections.filtered(lambda record: record.move_type == 'out_invoice')
        alignment = corrections.filtered(lambda record: record.move_type == 'entry')
        self.assertEqual(len(alignment), 1)
        self.assertEqual(alignment.state, 'draft')
        self.assertAlmostEqual(sum(alignment.line_ids.mapped('balance')), 0, places=2)
        self.assertEqual(credit.amount_tax, 0)
        self.assertAlmostEqual(replacement.amount_tax, 12.28, places=2)
        for account in (self.receivable, self.income, self.tax_account):
            combined = (move | credit | alignment).line_ids.filtered(lambda line: line.account_id == account)
            self.assertAlmostEqual(sum(combined.mapped('balance')), 0, places=2)
        self.assertEqual(move.e_invoice_uuid, receipt['uuid'])
        self.assertTrue(saved.exists())

    def test_invalid_receipt_is_archived_before_same_invoice_reopens(self):
        move = self.vat_invoice()
        receipt = self.receipt(move, status='Invalid')
        move.write({'e_invoice_uuid': receipt['uuid'], 'e_invoice_status': 'Invalid', 'e_invoice_sent': True})
        move.eta_job_ids.write({'state': 'invalid', 'attempts': 1, 'attempted_at': fields.Datetime.now()})
        attachment = self.env['ir.attachment'].create({'name': 'ETA_invalid_retained.json', 'res_model': 'account.move',
            'res_id': move.id, 'mimetype': 'application/json', 'raw': json.dumps({
                'request': receipt['document'], 'response': {'l10n_eg_uuid': receipt['uuid']}}).encode()})
        move.l10n_eg_eta_json_doc_id = attachment
        with patch('odoo.addons.tradeline_eta_reliability.models.draft_correction.EtaClient.request', return_value=receipt):
            action = self.vat_wizard(move).action_prepare()
        self.assertEqual(action['res_id'], move.id)
        self.assertEqual(move.state, 'draft')
        self.assertFalse(move.e_invoice_uuid)
        self.assertAlmostEqual(move.amount_tax, 12.28, places=2)
        self.assertEqual(move.eta_job_ids.state, 'review')
        archive = self.env['ir.attachment'].search([('res_model', '=', 'account.move'), ('res_id', '=', move.id),
            ('name', '=', 'ETA_VAT_correction_receipt_%s.json' % receipt['uuid'])])
        self.assertEqual(json.loads(archive.raw)['status'], 'Invalid')

    def test_wrong_or_pending_receipt_cannot_prepare_correction(self):
        for change in ({'internalId': 'OTHER-INVOICE'}, {'status': 'Submitted'}, {'status': 'Cancelled'}):
            with self.subTest(change=change):
                move = self.vat_invoice()
                receipt = self.receipt(move)
                receipt.update(change)
                move.write({'e_invoice_uuid': 'VAT-ORIGINAL-RECEIPT', 'e_invoice_status': 'Valid'})
                move.eta_job_ids.state = 'valid'
                with patch('odoo.addons.tradeline_eta_reliability.models.draft_correction.EtaClient.request', return_value=receipt):
                    with self.assertRaises(UserError):
                        self.vat_wizard(move).action_prepare()
                self.assertEqual(move.state, 'posted')
                self.assertFalse(move.eta_vat_correction_ids)

    def test_uncertain_submission_blocks_vat_wizard_before_http(self):
        move = self.vat_invoice()
        move.eta_job_ids.write({'state': 'unknown', 'attempted_at': fields.Datetime.now()})
        with self.assertRaisesRegex(UserError, 'unresolved'):
            self.vat_wizard(move).action_prepare()
        self.assertEqual(move.state, 'posted')

    def prepare_valid_pair(self, edited=False):
        move = self.vat_invoice()
        receipt = self.receipt(move)
        if edited:
            move.button_draft()
            move.invoice_line_ids.tax_ids = self.vat
            move.action_post()
        move.write({'e_invoice_uuid': receipt['uuid'], 'e_invoice_status': 'Valid', 'e_invoice_sent': True})
        move.eta_job_ids.state = 'valid'
        with patch('odoo.addons.tradeline_eta_reliability.models.draft_correction.EtaClient.request', return_value=receipt):
            self.vat_wizard(move).action_prepare()
        return move, move.eta_vat_correction_ids.filtered(lambda record: record.move_type == 'out_refund'), \
            move.eta_vat_correction_ids.filtered(lambda record: record.move_type == 'out_invoice')

    def test_credit_serialization_references_original_authority_uuid(self):
        move, credit, replacement = self.prepare_valid_pair()
        values = credit._check_extra_fields({'documentType': 'C'})
        self.assertEqual(values['references'], [move.e_invoice_uuid])
        self.assertNotIn('references', replacement._check_extra_fields({'documentType': 'I'}))

    def test_replacement_waits_for_posted_credit_then_eta_validation(self):
        move, credit, replacement = self.prepare_valid_pair()
        with self.assertRaisesRegex(UserError, 'credit note'):
            replacement.action_post()
        credit.action_post()
        with self.assertRaisesRegex(UserError, 'Valid at ETA'):
            replacement.action_post()
        credit.write({'e_invoice_uuid': 'CORRECTION-CREDIT-UUID', 'e_invoice_status': 'Valid'})
        credit.eta_job_ids.state = 'valid'
        replacement.action_post()
        credit.e_invoice_status = 'Submitted'
        client = MagicMock()
        job = replacement.eta_job_ids
        self.assertFalse(job._preflight_submission(client))
        self.assertEqual(job.state, 'queued')
        self.assertFalse(job.attempted_at)
        client.request.assert_not_called()
        credit.write({'e_invoice_uuid': 'CORRECTION-CREDIT-UUID', 'e_invoice_status': 'Valid'})
        credit.eta_job_ids.state = 'valid'
        client.request.return_value = {'uuid': credit.e_invoice_uuid, 'internalId': credit.name, 'status': 'Valid'}
        with patch('odoo.addons.tradeline_eta_reliability.models.submission_safety.EtaSubmissionSafety._preflight_submission', return_value=True) as parent:
            self.assertTrue(job._preflight_submission(client))
            parent.assert_called_once()
        client.request.assert_called_once_with('GET', '/api/v1.0/documents/CORRECTION-CREDIT-UUID/raw')

    def test_posted_alignment_credit_and_original_net_to_zero_without_duplicate_vat(self):
        move, credit, replacement = self.prepare_valid_pair(edited=True)
        alignment = move.eta_vat_correction_ids.filtered(lambda record: record.move_type == 'entry')
        before = sorted((line.account_id.id, line.balance) for line in alignment.line_ids)
        alignment.action_post()
        credit.action_post()
        credit.write({'e_invoice_uuid': 'CORRECTION-CREDIT-UUID', 'e_invoice_status': 'Valid'})
        credit.eta_job_ids.state = 'valid'
        replacement.action_post()
        self.assertEqual(before, sorted((line.account_id.id, line.balance) for line in alignment.line_ids))
        for account in (self.receivable, self.income, self.tax_account):
            combined = (move | credit | alignment).line_ids.filtered(lambda line: line.account_id == account)
            self.assertAlmostEqual(sum(combined.mapped('balance')), 0, places=2)
        self.assertAlmostEqual(sum((move | credit | alignment | replacement).line_ids.filtered(
            lambda line: line.account_id == self.tax_account).mapped('balance')), -12.28, places=2)
        for tag in self.vat_base_tag | self.vat_tax_tag:
            combined = (move | credit | alignment).line_ids.filtered(lambda line: tag in line.tax_tag_ids)
            self.assertAlmostEqual(sum(line.balance * (-1 if line.tax_tag_invert else 1) for line in combined), 0, places=2)

    def test_invoice_user_can_prepare_vat_correction_without_administrator(self):
        user = new_test_user(self.env, login='vat_correction_clerk', groups='account.group_account_invoice',
            company_id=self.company.id, company_ids=[Command.set(self.company.ids)])
        self.assertFalse(user.has_group('base.group_system'))
        self.assertFalse(user.has_group('account.group_account_manager'))
        move = self.invoice().with_user(user)
        self.assertTrue(move.show_reset_to_draft_button)
        self.assertEqual(move.action_eta_correct_vat()['res_model'], 'tradeline.eta.vat.correction')
        wizard = self.env['tradeline.eta.vat.correction'].with_user(user).create({
            'move_id': move.id, 'tax_id': self.vat.id})
        action = wizard.action_prepare()
        self.assertEqual(action['res_id'], move.id)
        self.assertEqual(move.state, 'draft')
        self.assertGreater(move.amount_tax, 0)
        move.action_post()
        self.assertEqual(move.state, 'posted')
        self.assertEqual(move.eta_job_ids.state, 'queued')

    def test_non_accountant_cannot_prepare_vat_correction(self):
        user = new_test_user(self.env, login='vat_correction_unprivileged', groups='base.group_user',
            company_id=self.company.id, company_ids=[Command.set(self.company.ids)])
        move = self.invoice()
        with self.assertRaises(AccessError):
            self.env['tradeline.eta.vat.correction'].with_user(user).create({'move_id': move.id, 'tax_id': self.vat.id})

    def test_confirmed_eta_cancellation_keeps_standard_draft_flow(self):
        move = self.vat_invoice()
        receipt = self.receipt(move, status='Cancelled')
        move.write({'e_invoice_uuid': receipt['uuid'], 'e_invoice_status': 'Cancelled', 'e_invoice_sent': True})
        move.eta_job_ids.write({'state': 'invalid', 'attempts': 1, 'attempted_at': fields.Datetime.now(),
                               'submission_uuid': 'cancelled-submission'})
        attachment = self.env['ir.attachment'].create({'name': 'ETA_cancelled_retained.json', 'res_model': 'account.move',
            'res_id': move.id, 'mimetype': 'application/json', 'raw': json.dumps({
                'request': receipt['document'], 'response': {'l10n_eg_uuid': receipt['uuid']}}).encode()})
        move.l10n_eg_eta_json_doc_id = attachment
        with patch('odoo.addons.tradeline_eta_reliability.models.draft_correction.EtaClient.request', return_value=receipt):
            move.button_draft()
        self.assertEqual(move.state, 'draft')
        self.assertFalse(move.e_invoice_uuid)
        self.assertEqual(move.eta_job_ids.state, 'blocked')
        self.assertIn('cancelled', move.eta_job_ids.last_error)

    def test_edited_cancellation_status_cannot_erase_valid_eta_receipt(self):
        move = self.vat_invoice()
        receipt = self.receipt(move)
        move.write({'e_invoice_uuid': receipt['uuid'], 'e_invoice_status': 'Cancelled', 'e_invoice_sent': True})
        move.eta_job_ids.state = 'valid'
        with patch('odoo.addons.tradeline_eta_reliability.models.draft_correction.EtaClient.request', return_value=receipt):
            with self.assertRaisesRegex(UserError, 'not confirmed cancellation'):
                move.button_draft()
        self.assertEqual(move.state, 'posted')
        self.assertEqual(move.e_invoice_uuid, receipt['uuid'])

    def test_retained_receipt_prevents_reset_when_active_pointers_are_missing(self):
        move = self.vat_invoice()
        receipt = self.receipt(move)
        self.env['ir.attachment'].create({'name': 'ETA_retained.json', 'res_model': 'account.move',
            'res_id': move.id, 'mimetype': 'application/json', 'raw': json.dumps({
                'request': receipt['document'], 'response': {'l10n_eg_uuid': receipt['uuid']}}).encode()})
        self.assertFalse(move._eta_can_reset_unsent())
        with self.assertRaisesRegex(UserError, 'receipt|unresolved'):
            move.button_draft()
        self.assertEqual(move.state, 'posted')

    def test_alignment_uses_original_period_and_replacement_uses_today(self):
        move = self.vat_invoice()
        receipt = self.receipt(move)
        move.button_draft()
        original_date = fields.Date.today() - timedelta(days=10)
        move.write({'invoice_date': original_date, 'date': original_date})
        move.invoice_line_ids.tax_ids = self.vat
        move.action_post()
        move.write({'e_invoice_uuid': receipt['uuid'], 'e_invoice_status': 'Valid', 'e_invoice_sent': True})
        move.eta_job_ids.state = 'valid'
        with patch('odoo.addons.tradeline_eta_reliability.models.draft_correction.EtaClient.request', return_value=receipt):
            self.vat_wizard(move).action_prepare()
        alignment = move.eta_vat_correction_ids.filtered(lambda record: record.move_type == 'entry')
        self.assertEqual(alignment.date, original_date)
        for record in move.eta_vat_correction_ids.filtered(lambda record: record.move_type != 'entry'):
            self.assertEqual(record.invoice_date, fields.Date.today())

    def test_correction_keeps_invoice_company_when_another_company_is_active(self):
        move = self.vat_invoice()
        receipt = self.receipt(move)
        move.button_draft()
        move.invoice_line_ids.tax_ids = self.vat
        move.action_post()
        move.write({'e_invoice_uuid': receipt['uuid'], 'e_invoice_status': 'Valid', 'e_invoice_sent': True})
        move.eta_job_ids.state = 'valid'
        wizard = self.vat_wizard(move).with_context(allowed_company_ids=[self.original_company.id, self.company.id])
        self.assertNotEqual(wizard.env.company, move.company_id)
        with patch('odoo.addons.tradeline_eta_reliability.models.draft_correction.EtaClient.request', return_value=receipt):
            wizard.action_prepare()
        self.assertEqual(len(move.eta_vat_correction_ids), 3)
        self.assertEqual(move.eta_vat_correction_ids.company_id, move.company_id)

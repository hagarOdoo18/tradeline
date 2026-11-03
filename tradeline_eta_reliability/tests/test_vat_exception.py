import json
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from lxml import etree
from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user

from .test_vat_review import TestEtaVatReview
from ..models.draft_correction import EtaVatCorrectionTransport


@tagged('post_install', '-at_install')
class TestEtaVatException(TestEtaVatReview):

    def exemption(self, code='V007', native=False):
        tax_type = self.vat.type_code_id
        subtype = self.env['account.tax.sub.type.code'].search([('code', '=', code)], limit=1)
        subtype = subtype or self.env['account.tax.sub.type.code'].create({
            'code': code, 'ar_name': code, 'type_id': tax_type.id})
        values = {'name': 'QA explicit ' + code + (' native' if native else ''), 'amount': 0, 'company_id': self.company.id,
            'type_tax_use': 'sale', 'amount_type': 'percent'}
        values.update({'l10n_eg_eta_code': 't1_' + code.lower()} if native else {
            'type_code_id': tax_type.id, 'sub_type_code_id': subtype.id})
        return self.env['account.tax'].create(values)

    def exempt_invoice(self, code='V007', native=False):
        self.vat = self.exemption(code, native)
        return self.invoice(vat=True)

    def prepare(self, move, subtype=None):
        document = self.payload(move)
        if subtype:
            document['invoiceLines'][0]['taxableItems'] = [
                {'taxType': 'T1', 'subType': subtype, 'amount': 0, 'rate': 0}]
        job = move.eta_job_ids.sudo()
        job.payload = json.dumps({'documents': [document]})
        return job

    def test_agreements_exemption_reaches_preflight_without_accounting_changes(self):
        move = self.exempt_invoice()
        before = (move.amount_total, move.amount_tax, move.invoice_line_ids.tax_ids, move.invoice_date)
        job = self.prepare(move, 'V007')
        with patch.object(EtaVatCorrectionTransport, '_preflight_submission', return_value=True) as remaining:
            self.assertTrue(job._preflight_submission(MagicMock()))
        remaining.assert_called_once()
        self.assertEqual(move.eta_vat_review_state, 'exempt')
        self.assertFalse(move.eta_vat_workflow)
        self.assertNotIn(move, self.env['account.move'].search([('eta_vat_workflow', '=', 'unsent')]))
        self.assertEqual(before, (move.amount_total, move.amount_tax, move.invoice_line_ids.tax_ids, move.invoice_date))
        self.assertFalse(job.attempts)
        job.write({'state':'blocked', 'last_error':'VAT is missing. Nothing was sent to ETA.'})
        self.assertIn('explicit 0% VAT exemption', job.staff_summary)
        self.assertIn('Sync', job.required_action)

    def test_native_exemption_is_recognized(self):
        move = self.exempt_invoice(native=True)
        self.assertTrue(move._eta_exemption_codes())
        self.assertTrue(self.prepare(move, 'V007')._assert_positive_vat_submission())

    def test_approved_exemption_is_never_advertised_as_needing_fourteen_percent(self):
        move = self.exempt_invoice()
        job = self.prepare(move, 'V007')
        document = json.loads(job.payload)['documents'][0]
        uuid = 'approved-zero-exemption'
        move.write({'e_invoice_uuid': uuid, 'e_invoice_status': 'Valid', 'e_invoice_sent': True})
        job.state = 'valid'
        archive = self.env['ir.attachment'].create({'name': 'ETA_%s.json' % uuid,
            'res_model': 'account.move', 'res_id': move.id, 'company_id': self.company.id,
            'mimetype': 'application/json',
            'raw': json.dumps({'request': document, 'response': {'l10n_eg_uuid': uuid}}).encode()})
        move.l10n_eg_eta_json_doc_id = archive
        self.assertTrue(move._eta_record_receipt_vat(uuid, document))
        before = (move.invoice_date, move.amount_total, move.amount_tax, job.payload, job.attempts)
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Rendering contacted ETA')):
            self.assertEqual(move.eta_vat_review_state, 'exempt')
            self.assertEqual(job.vat_review_state, 'exempt')
            self.assertEqual(move.eta_vat_workflow, 'exempt')
            for route, present in [('exempt', True), ('approved', False)]:
                found = self.env['account.move'].with_user(self.clerk).search([
                    ('id', '=', move.id), ('eta_vat_workflow', '=', route)])
                self.assertEqual(bool(found), present)
        self.assertEqual(before, (move.invoice_date, move.amount_total, move.amount_tax, job.payload, job.attempts))
        # A conflicting identity must retain the review route, even with an
        # explicit exemption. Do not hide uncertainty behind a green label.
        move.l10n_eg_uuid = 'conflicting-receipt'
        self.assertEqual(move.eta_vat_workflow, 'review')
        self.assertIn(move, self.env['account.move'].search([
            ('id', '=', move.id), ('eta_vat_workflow', '=', 'review')]))

    def test_ahmed_serializer_preserves_explicit_zero_exemption_and_totals(self):
        for native in (False, True):
            move = self.exempt_invoice(native=native)
            move.button_draft()
            metadata = {'code_type':'egs', 'e_invoicing_code':'EG-123456789-VOUCHER'}
            product = self.env['product.product'].create({'name':'Exempt voucher',
                **{key:value for key,value in metadata.items() if key in self.env['product.product']._fields}})
            unit = self.env['eta.uom'].search([('code','=','D41')], limit=1)
            product.uom_id.eta_uom_id = unit or self.env['eta.uom'].create({'name':'Each','code':'D41'})
            move.invoice_line_ids.write({'product_id':product.id, 'tax_ids':[Command.set(self.vat.ids)], 'price_unit':100})
            move.action_post()
            with ExitStack() as stack:
                for key,value in metadata.items():
                    if key not in product._fields:
                        stack.enter_context(patch.object(type(product), key, value, create=True))
                lines, discount, sales, total = move._get_eta_invoice_lines()
            self.assertEqual(total, move.amount_total)
            self.assertEqual(lines[0]['taxableItems'], [
                {'taxType':'T1','subType':'V007','amount':0,'rate':0}])

    def test_native_eta_network_is_disabled_for_every_operation(self):
        edi = self.env.ref('l10n_eg_edi_eta.edi_eg_eta')
        with patch('requests.sessions.Session.request') as http:
            for method in ('POST','GET','PUT'):
                result = edi._l10n_eg_eta_connect_to_server({}, '/unused', method)
                self.assertEqual(result['blocking_level'],'error')
                self.assertIn('Ahmed Salama', result['error'])
        http.assert_not_called()

    def test_old_native_sign_action_routes_to_ahmed_staff_flow(self):
        move = self.invoice().with_user(self.clerk)
        with patch.object(type(move), 'action_eta_send_now', return_value={'tag':'ahmed-flow'}) as send:
            self.assertEqual(move.action_post_sign_invoices(), {'tag':'ahmed-flow'})
        send.assert_called_once()

    def test_native_thumb_drive_and_old_signature_callback_cannot_sign(self):
        Drive = self.env['l10n_eg_edi.thumb.drive']
        with self.assertRaisesRegex(UserError, 'native ETA signing is disabled'):
            Drive.action_sign_invoices(self.env['account.move'])
        with self.assertRaisesRegex(UserError, 'native ETA signing is disabled'):
            Drive.set_signature_data('{}')

    def test_zero_general_sales_is_not_an_exemption(self):
        move = self.exempt_invoice('V009')
        with self.assertRaisesRegex(UserError, 'VAT is missing'):
            self.prepare(move, 'V009')._assert_positive_vat_submission()

    def test_zero_tax_without_metadata_and_forged_context_remains_blocked(self):
        move = self.invoice()
        job = self.prepare(move)
        with self.assertRaisesRegex(UserError, 'VAT is missing'):
            job.with_context(bypass_vat=True, eta_vat_bypass=True)._assert_positive_vat_submission()

    def test_one_exempt_line_does_not_cover_an_unclassified_line(self):
        move = self.exempt_invoice()
        # The source classification itself must cover every invoice line.
        move.button_draft()
        move.invoice_line_ids = [Command.create({'name': 'Unclassified', 'account_id': self.income.id,
            'quantity': 1, 'price_unit': 50, 'tax_ids': [Command.clear()]})]
        move.action_post()
        self.assertFalse(move._eta_exemption_codes())
        with self.assertRaisesRegex(UserError, 'VAT is missing'):
            self.prepare(move, 'V007')._assert_positive_vat_submission()

    def test_missing_or_changed_exemption_in_prepared_payload_is_blocked(self):
        move = self.exempt_invoice()
        for subtype in (None, 'V003'):
            with self.assertRaisesRegex(UserError, 'VAT is missing'):
                self.prepare(move, subtype)._assert_positive_vat_submission()

    def test_exemption_never_skips_remaining_receipt_checks(self):
        job = self.prepare(self.exempt_invoice(), 'V007')
        with patch.object(EtaVatCorrectionTransport, '_preflight_submission', return_value=False):
            self.assertFalse(job._preflight_submission(MagicMock()))

    def test_bypass_requires_reason_and_records_private_approval_without_sending(self):
        move = self.invoice().with_user(self.clerk)
        with self.assertRaises(UserError):
            move._eta_approve_vat_bypass('  ')
        before = (move.amount_total, move.amount_tax, move.invoice_date, move.eta_job_ids.attempts)
        with patch('odoo.addons.tradeline_eta_reliability.models.client.EtaClient.request') as http:
            move._eta_approve_vat_bypass('Accounting approved zero VAT for voucher settlement')
        http.assert_not_called()
        self.assertTrue(move._eta_vat_bypass_current())
        self.assertEqual(move.eta_vat_bypass_by, self.clerk)
        self.assertEqual(move.eta_vat_review_state, 'bypass')
        self.assertTrue(self.prepare(move).sudo()._assert_positive_vat_submission())
        self.assertEqual(before, (move.amount_total, move.amount_tax, move.invoice_date, move.eta_job_ids.attempts))
        audit = self.env['ir.attachment'].sudo().search([('res_model','=','account.move'),
            ('res_id','=',move.id), ('name','=','ETA_VAT_bypass_approval.json')])
        self.assertEqual(len(audit), 1)
        self.assertFalse(audit.public)

    def test_bypass_approval_cannot_be_forged_or_copied(self):
        move = self.invoice()
        with self.assertRaises(AccessError):
            move.with_user(self.clerk).write({'eta_vat_bypass_reason': 'fake', 'eta_vat_bypass_hash': 'fake'})
        with self.assertRaises(AccessError):
            self.env['account.move'].create({'eta_vat_bypass_reason': 'fake'})
        move.with_user(self.clerk)._eta_approve_vat_bypass('Reviewed exception')
        copied = move.copy()
        self.assertFalse(copied.eta_vat_bypass_reason)
        self.assertFalse(copied._eta_vat_bypass_current())

    def test_bypass_requires_workspace_and_invoice_permissions(self):
        move = self.invoice()
        ordinary = new_test_user(self.env, login='vat_no_workspace', groups='account.group_account_invoice')
        readonly = new_test_user(self.env, login='vat_workspace_reader',
            groups='account.group_account_readonly,tradeline_eta_reliability.group_eta_workspace')
        for user in (ordinary, readonly):
            with self.assertRaises(AccessError):
                move.with_user(user).action_eta_send_without_vat()

    def test_invoice_edit_invalidates_bypass(self):
        move = self.invoice()
        move.with_user(self.clerk)._eta_approve_vat_bypass('Reviewed exception')
        move.button_draft()
        move.partner_id = self.partner.copy({'name':'Different buyer'})
        move.action_post()
        self.assertFalse(move._eta_vat_bypass_current())
        self.assertEqual(move.eta_vat_review_state, 'missing')
        with self.assertRaisesRegex(UserError, 'VAT is missing'):
            self.prepare(move)._assert_positive_vat_submission()

    def test_bypass_does_not_allow_taxed_ledger_with_zero_payload(self):
        move = self.invoice(vat=True)
        move.with_user(self.clerk)._eta_approve_vat_bypass('Reviewed exception')
        with self.assertRaisesRegex(UserError, 'prepared ETA document'):
            self.prepare(move)._assert_positive_vat_submission()

    def test_wizard_uses_existing_send_flow_and_preserves_unknown_history(self):
        move = self.invoice()
        job = move.eta_job_ids
        job.write({'state':'unknown','attempts':1,'attempted_at':fields.Datetime.now()})
        wizard = self.env['tradeline.eta.vat.bypass'].with_user(self.clerk).create({
            'move_id':move.id, 'reason':'Reviewed exception'})
        with patch.object(type(move), 'action_eta_send_now', return_value={'tag':'existing-flow'}) as send:
            self.assertEqual(wizard.action_send(), {'tag':'existing-flow'})
        send.assert_called_once()
        self.assertEqual(job.state, 'unknown')
        self.assertEqual(job.attempts, 1)
        self.assertTrue(job.attempted_at)

    def test_known_approval_cannot_be_resent_with_bypass(self):
        move = self.invoice()
        move.write({'e_invoice_uuid':'approved','e_invoice_status':'Valid'})
        with self.assertRaises(UserError):
            move.with_user(self.clerk).action_eta_send_without_vat()

    def test_bypass_buttons_and_reason_wizard_compile(self):
        for model, method in (('account.move','action_eta_send_without_vat'),
                ('tradeline.eta.job','action_send_without_vat')):
            arch = self.env[model].with_user(self.clerk).get_view(view_type='form')['arch']
            self.assertTrue(etree.fromstring(arch).xpath("//button[@name='%s']" % method))
            if model == 'account.move':
                root = etree.fromstring(arch)
                self.assertTrue(root.xpath("//div[@role='status']//field[@name='eta_staff_reason']"))
                self.assertEqual(root.xpath("//button[@name='action_post_sign_invoices']/@invisible"), ['1'])
        arch = self.env['tradeline.eta.vat.bypass'].with_user(self.clerk).get_view(view_type='form')['arch']
        self.assertTrue(etree.fromstring(arch).xpath("//field[@name='reason']"))

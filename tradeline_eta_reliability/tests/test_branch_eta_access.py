from unittest.mock import patch

from lxml import etree

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user


@tagged('post_install', '-at_install')
class TestEtaBranchPrivacy(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.central = new_test_user(cls.env, login='eta_privacy_central',
            groups='account.group_account_invoice,tradeline_eta_reliability.group_eta_workspace')
        cls.branch = new_test_user(cls.env, login='eta_privacy_branch', groups='account.group_account_invoice')
        cls.branch_admin = new_test_user(cls.env, login='eta_privacy_branch_admin',
            groups='account.group_account_invoice,base.group_system')
        cls.receivable = cls.env['account.account'].create({'name': 'Privacy QA Receivable',
            'code': 'PRIVAR', 'account_type': 'asset_receivable', 'reconcile': True,
            'company_ids': [Command.set(cls.company.ids)]})
        cls.income = cls.env['account.account'].create({'name': 'Privacy QA Income',
            'code': 'PRIVIN', 'account_type': 'income', 'company_ids': [Command.set(cls.company.ids)]})
        cls.journal = cls.env['account.journal'].create({'name': 'Privacy QA Sales', 'code': 'PRIV',
            'type': 'sale', 'company_id': cls.company.id, 'edi_format_ids': [Command.clear()]})
        cls.partner = cls.env['res.partner'].create({'name': 'Privacy QA Buyer',
            'property_account_receivable_id': cls.receivable.id})
        cls.eta = cls.env.ref('l10n_eg_edi_eta.edi_eg_eta')

    def setUp(self):
        super().setUp()
        guard = patch('requests.sessions.Session.request', side_effect=AssertionError('Privacy test attempted HTTP'))
        guard.start()
        self.addCleanup(guard.stop)

    def invoice(self):
        move = self.env['account.move'].with_context(eta_queue_cron=True).create({
            'move_type': 'out_invoice', 'journal_id': self.journal.id, 'partner_id': self.partner.id,
            'invoice_date': fields.Date.today(), 'invoice_line_ids': [Command.create({
                'name': 'Privacy QA item', 'account_id': self.income.id, 'quantity': 1,
                'price_unit': 100, 'tax_ids': [Command.clear()]})]})
        move.action_post()
        return move

    def test_branch_forms_hide_eta_controls_and_ribbons_including_developer_users(self):
        for user in (self.branch, self.branch_admin):
            form = etree.fromstring(self.env['account.move'].with_user(user).get_view(view_type='form')['arch'])
            self.assertTrue(form.xpath("//field[@name='invoice_line_ids']"))
            self.assertTrue(form.xpath("//header/button[@name='action_post']"))
            self.assertTrue(form.xpath("//field[@name='name']"))
            self.assertFalse(form.xpath("//button[@name='action_eta_send_now']"))
            self.assertFalse(form.xpath("//button[@name='action_eta_check_status_now']"))
            self.assertFalse(form.xpath("//button[@name='action_cancel_electronic_invoice']"))
            self.assertFalse(form.xpath("//page[.//field[@name='e_invoice_status']]"))
            self.assertFalse(form.xpath("//widget[contains(@title,'E-I')]"))
            self.assertFalse(form.xpath("//div[@role='status'][.//field[@name='eta_staff_reason']]"))
            self.assertFalse(form.xpath("//page[@name='page_edi_documents']"))
            self.assertFalse(form.xpath("//page[@name='page_eta_e_invoice']"))
            self.assertFalse(form.xpath("//page[.//field[starts-with(@name,'l10n_eg_')]]"))
            self.assertFalse(form.xpath("//button[@name='action_post_sign_invoices' or @name='action_get_eta_invoice_pdf']"))
            bindings = self.env['ir.actions.actions'].with_user(user).get_bindings('account.move')
            sign_id = self.env.ref('l10n_eg_edi_eta.action_sign_invoices').id
            self.assertFalse(any(action['id'] == sign_id for action in bindings.get('action', [])))

    def test_branch_compiled_native_eta_metadata_is_hidden_even_through_sudo(self):
        for user in (self.branch, self.branch_admin):
            Move = self.env['account.move'].with_user(user)
            for caller in (Move, Move.sudo()):
                form = etree.fromstring(caller.get_view(view_type='form')['arch'])
                self.assertFalse(form.xpath("//page[@name='page_eta_e_invoice']"))
                self.assertFalse(form.xpath("//button[@name='action_post_sign_invoices' or @name='action_get_eta_invoice_pdf']"))
                for field in form.xpath('//field'):
                    if field.get('name', '').startswith(('eta_', 'l10n_eg_', 'e_invoice_')):
                        self.assertIn(field.get('invisible'), ('1', 'True', 'true'), field.get('name'))
                sign_id = self.env.ref('l10n_eg_edi_eta.action_sign_invoices').id
                bindings = caller.env['ir.actions.actions'].get_bindings('account.move')
                self.assertFalse(any(action['id'] == sign_id for action in bindings.get('action', [])))

    def test_central_clerk_keeps_eta_controls_without_accounting_manager(self):
        self.assertFalse(self.central.has_group('account.group_account_manager'))
        form = etree.fromstring(self.env['account.move'].with_user(self.central).get_view(view_type='form')['arch'])
        self.assertTrue(form.xpath("//button[@name='action_eta_send_now'][@string='Sync']"))
        self.assertTrue(form.xpath("//page//button[@name='action_eta_check_status_now'][@string='Update Status']"))
        self.assertTrue(form.xpath("//widget[@title='E-I Valid']"))
        self.assertTrue(form.xpath("//page[@name='page_eta_e_invoice']"))
        self.assertTrue(form.xpath("//button[@name='action_get_eta_invoice_pdf']"))

    def test_branch_invoice_and_credit_lists_hide_eta_columns_and_filters(self):
        for user in (self.branch, self.branch_admin):
            Move = self.env['account.move'].with_user(user)
            for view in ('account.view_out_invoice_tree', 'account.view_out_credit_note_tree'):
                root = etree.fromstring(Move.get_view(view_id=self.env.ref(view).id, view_type='list')['arch'])
                self.assertTrue(root.xpath("//field[@name='name']"))
                self.assertFalse(root.xpath("//field[@name='e_invoice_status']"))
                self.assertFalse(root.xpath("//field[@name='eta_staff_state']"))
                self.assertFalse(root.xpath("//field[@name='expired_flag']"))
                self.assertFalse(root.xpath("//button[@name='action_eta_send_now']"))
            root = etree.fromstring(Move.get_view(view_id=self.env.ref('account.view_account_invoice_filter').id,
                                                  view_type='search')['arch'])
            for name in ('submitted', 'valid', 'invalid', 'eta_missing_tracking'):
                self.assertFalse(root.xpath("//filter[@name='%s']" % name))

    def test_branch_staff_entry_points_are_denied_even_through_sudo_metadata(self):
        move = self.invoice()
        actions = ('action_eta_send_now', 'action_eta_check_status_now',
                   'action_send_electronic_invoice', 'action_update_electronic_invoice_status',
                   'action_cancel_electronic_invoice', 'action_generate_eta_json',
                   'action_sign_invoice', 'action_update_electronic_invoice_pdf',
                   'action_post_sign_invoices', 'action_get_eta_invoice_pdf')
        for user in (self.branch, self.branch_admin):
            for caller in (move.with_user(user), move.with_user(user).sudo()):
                for name in actions:
                    with self.assertRaises(AccessError):
                        getattr(caller, name)()
                with self.assertRaises(AccessError):
                    caller.action_eta_execute_staff_request(0)

    def test_native_eta_notices_are_hidden_but_other_edi_notices_stay(self):
        move = self.invoice()
        native = self.env['account.edi.document'].create({'move_id': move.id, 'edi_format_id': self.eta.id,
            'state': 'to_send', 'error': 'ETA internal notice', 'blocking_level': 'info'})
        branch = move.with_user(self.branch)
        before = native.read(['state', 'error', 'blocking_level'])
        self.assertTrue(branch.eta_branch_hide_native_controls)
        self.assertEqual(branch.edi_error_count, 0)
        self.assertFalse(branch.edi_error_message)
        self.assertFalse(branch.edi_web_services_to_process)
        self.assertEqual(move.with_user(self.central).edi_error_count, 1)
        self.assertEqual(before, native.read(['state', 'error', 'blocking_level']))
        with self.assertRaises(AccessError):
            branch.button_process_edi_web_services()
        other_format = self.env['account.edi.format'].create({'name': 'Other privacy EDI', 'code': 'other_privacy_qa'})
        other = self.env['account.edi.document'].create({'move_id': move.id, 'edi_format_id': other_format.id,
            'state': 'to_send', 'error': 'Other integration message', 'blocking_level': 'info'})
        with patch.object(type(self.eta), '_needs_web_services', return_value=True):
            self.assertEqual(branch.edi_error_count, 1)
            self.assertIn('Other integration message', branch.edi_error_message)
            self.assertNotIn('ETA internal notice', branch.edi_error_message)
            self.assertEqual(branch.edi_web_services_to_process, 'Other privacy EDI')
        process = 'odoo.addons.account_edi.models.account_edi_document.AccountEdiDocument._process_documents_web_services'
        with patch(process, autospec=True, return_value=0) as dispatcher:
            branch.button_process_edi_web_services()
            self.assertEqual(dispatcher.call_args.args[0].ids, other.ids)
        self.assertEqual(before, native.read(['state', 'error', 'blocking_level']))

    def test_eta_tracking_is_hidden_without_hiding_financial_tracking(self):
        move = self.invoice()
        Value = self.env['mail.tracking.value']
        message = move.message_post(body='Invoice validated', subtype_xmlid='mail.mt_note')
        eta = Value.create({'field_id': self.env['ir.model.fields']._get('account.move', 'e_invoice_uuid').id,
                            'old_value_char': '', 'new_value_char': 'PRIVATE-ETA-UUID', 'mail_message_id': message.id})
        name = Value.create({'field_id': self.env['ir.model.fields']._get('account.move', 'name').id,
                             'old_value_char': '/', 'new_value_char': move.name, 'mail_message_id': message.id})
        values = eta | name
        for user in (self.branch, self.branch_admin):
            self.assertEqual(move.with_user(user)._track_filter_for_display(values).ids, name.ids)
        self.assertEqual(move.with_user(self.central)._track_filter_for_display(values).ids, values.ids)

    def test_branch_draft_guard_uses_a_plain_accounting_message_and_keeps_receipt(self):
        move = self.invoice()
        move.write({'e_invoice_uuid': 'unchanged-receipt', 'e_invoice_status': 'Valid', 'e_invoice_sent': True})
        with self.assertRaisesRegex(UserError, 'Central accounting must review'):
            move.with_user(self.branch).button_draft()
        self.assertEqual(move.state, 'posted')
        self.assertEqual(move.e_invoice_uuid, 'unchanged-receipt')

    def test_branch_chatter_fetch_hides_only_unmixed_eta_operation_notes(self):
        move = self.invoice()
        eta_note = move.message_post(body='ETA direct send requested by Accountant: Approved by ETA. Done.',
                                     subtype_xmlid='mail.mt_note')
        finance_note = move.message_post(body='Payment reconciled by accounting.', subtype_xmlid='mail.mt_note')
        mixed = move.message_post(body='ETA status check requested by Accountant: financial correction reviewed.',
                                  subtype_xmlid='mail.mt_note')
        self.env['mail.tracking.value'].create({
            'field_id': self.env['ir.model.fields']._get('account.move', 'name').id,
            'old_value_char': '/', 'new_value_char': move.name, 'mail_message_id': mixed.id})
        attachment = self.env['ir.attachment'].create({'name': 'Accounting review', 'raw': b'QA only'})
        attached = move.message_post(body='ETA status check requested by Accountant: reviewed document.',
                                     subtype_xmlid='mail.mt_note', attachment_ids=attachment.ids)
        domain = [('model', '=', 'account.move'), ('res_id', '=', move.id)]
        for user in (self.branch, self.branch_admin):
            messages = self.env['mail.message'].with_user(user)._message_fetch(domain)['messages']
            self.assertNotIn(eta_note.id, messages.ids)
            self.assertIn(finance_note.id, messages.ids)
            self.assertIn(mixed.id, messages.ids)
            self.assertIn(attached.id, messages.ids)
        central = self.env['mail.message'].with_user(self.central)._message_fetch(domain)['messages']
        self.assertIn(eta_note.id, central.ids)

    def test_future_eta_tracking_is_private_while_invoice_state_tracking_remains(self):
        for user in (self.branch, self.branch_admin, self.central):
            tracked = self.env['account.move'].with_user(user)._track_get_fields()
            self.assertIn('state', tracked)
            self.assertFalse(any(name.startswith(('e_invoice_', 'eta_', 'l10n_eg_')) for name in tracked))

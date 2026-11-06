import json
import base64
import hashlib
from asn1crypto import cms
from contextlib import ExitStack
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user

from ..models.client import EtaClient, EtaError
from ..models.eta_monitoring import error_details


@tagged('post_install', '-at_install')
class TestEtaReliability(TransactionCase):

    def test_daily_monitor_excludes_backlog_even_after_new_job_and_issue_date(self):
        now = fields.Datetime.now()
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('tradeline_eta_reliability.daily_alert_start_date', str(fields.Date.today()))
        monitor = self.env['tradeline.operations.monitor'].with_context(active_test=False).search([], limit=1)
        old = self.invoice(invoice_date=fields.Date.today() - timedelta(days=30)).eta_job_ids
        old.write({'state': 'blocked', 'issue_date_policy': 'current', 'issue_date_approved_at': now})
        new = self.invoice().eta_job_ids
        # Historical recovery flags also attach to current-day failures.
        new.write({'state': 'invalid', 'historical_review': True, 'historical_fingerprint': 'new-daily-failure'})
        domain = monitor._eta_daily_alert_domain(now)
        self.assertNotIn(old, self.env['tradeline.eta.job'].search(domain))
        self.assertIn(new, self.env['tradeline.eta.job'].search(domain))
        # A genuinely old invoice re-dated today must still stay out.
        self.env.cr.execute('UPDATE account_move SET create_date=%s WHERE id=%s',
                            [now - timedelta(days=30), new.move_id.id])
        new.move_id.invalidate_recordset(['create_date'])
        self.assertNotIn(new, self.env['tradeline.eta.job'].search(domain))

    def test_daily_monitor_cutover_is_cairo_midnight_and_does_not_roll_forward(self):
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('tradeline_eta_reliability.daily_alert_start_date', '')
        monitor = self.env['tradeline.operations.monitor'].with_context(active_test=False).search([], limit=1)
        now = fields.Datetime.to_datetime('2026-11-05 22:30:00')
        domain = monitor._eta_daily_alert_domain(now)
        self.assertEqual(params.get_param('tradeline_eta_reliability.daily_alert_start_date'), '2026-11-06')
        self.assertEqual(domain[0][2], fields.Datetime.to_datetime('2026-11-05 22:00:00'))
        self.assertEqual(domain, monitor._eta_daily_alert_domain(now + timedelta(days=3)))
        params.set_param('tradeline_eta_reliability.daily_alert_start_date', '2026-07-06')
        self.assertEqual(monitor._eta_daily_alert_domain(now)[0][2],
                         fields.Datetime.to_datetime('2026-07-05 21:00:00'))

    def test_backlog_only_cannot_raise_daily_rejection_stall_or_progress(self):
        now = fields.Datetime.now()
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('tradeline_eta_reliability.daily_alert_start_date', str(fields.Date.today()))
        params.set_param('tradeline_eta_reliability.enabled_database', self.env.cr.dbname)
        params.set_param('tradeline_eta_reliability.last_processed_at', str(now - timedelta(hours=1)))
        self.env.ref('tradeline_eta_reliability.cron_eta_queue').write({'active': True, 'nextcall': now + timedelta(minutes=1)})
        monitor = self.env['tradeline.operations.monitor'].with_context(active_test=False).search([], limit=1)
        old = self.invoice(invoice_date=fields.Date.today() - timedelta(days=30)).eta_job_ids
        old.write({'state': 'blocked'})
        daily_codes = lambda: {item['code'] for item in monitor._probe_crons(now) if item['code'].startswith('eta:daily_')}
        self.assertFalse(daily_codes())
        old.write({'state': 'queued', 'next_attempt': now - timedelta(hours=1)})
        with patch.object(type(old), '_attention_domain', return_value=[('id', '=', old.id)]), patch.object(type(old), '_native_partial_progress_is_recent', return_value=False):
            self.assertFalse(daily_codes())
        new = self.invoice().eta_job_ids
        new.write({'state': 'blocked'})
        self.assertIn('eta:daily_rejected', daily_codes())
        new.write({'state': 'queued', 'next_attempt': now - timedelta(hours=1)})
        with patch.object(type(new), '_attention_domain', return_value=[('id', '=', new.id)]), patch.object(type(new), '_native_partial_progress_is_recent', return_value=False):
            self.assertTrue({'eta:daily_stalled', 'eta:daily_progress'} <= daily_codes())

    def test_legacy_backlog_retirement_is_silent_but_daily_recovery_notifies(self):
        from odoo.addons.tradeline_operations_monitor.models.operations_monitor import signal
        now = fields.Datetime.now()
        monitor = self.env['tradeline.operations.monitor'].with_context(active_test=False).search([], limit=1)
        monitor.confirmation_checks = 1
        monitor.incident_ids.unlink()
        incidents = self.env['tradeline.operations.incident']
        for code in ('eta:stalled', 'eta:rejected', 'eta:progress'):
            incidents |= incidents.create({'monitor_id': monitor.id, 'code': code, 'title': 'Historical backlog',
                'details': 'Old aggregate', 'severity': 'critical', 'state': 'open', 'last_notified_at': now})
        notice = monitor._reconcile([], {'crons'}, now)
        self.assertFalse(notice)
        self.assertEqual(set(incidents.mapped('state')), {'resolved'})
        self.assertTrue(all('does not mean the backlog was cleared' in item.details for item in incidents))
        notice = monitor._reconcile([signal('eta:daily_rejected', 'Daily invoices', '1 new invoice', 'Review')], {'crons'}, now)
        self.assertEqual(notice.mapped('code'), ['eta:daily_rejected'])
        notice = monitor._reconcile([], {'crons'}, now + timedelta(minutes=1))
        self.assertEqual(notice.mapped('code'), ['eta:daily_rejected'])
        self.assertEqual(notice.state, 'resolved')

    def test_daily_incident_link_keeps_all_history_available_outside_alert(self):
        monitor = self.env['tradeline.operations.monitor'].with_context(active_test=False).search([], limit=1)
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('tradeline_eta_reliability.daily_alert_start_date', str(fields.Date.today()))
        old = self.invoice(invoice_date=fields.Date.today() - timedelta(days=30)).eta_job_ids
        new = self.invoice().eta_job_ids
        (old | new).write({'state': 'blocked'})
        incident = self.env['tradeline.operations.incident'].create({'monitor_id': monitor.id,
            'code': 'eta:daily_rejected', 'title': 'Daily invoice', 'severity': 'warning'})
        action = incident.action_eta_invoice_log()
        self.assertIn(new, self.env['tradeline.eta.job'].search(action['domain']))
        self.assertNotIn(old, self.env['tradeline.eta.job'].search(action['domain']))
        self.assertEqual(monitor.action_eta_invoice_log()['res_model'], 'tradeline.eta.job')
        self.assertTrue(old.exists())

    def test_first_submission_candidate_cannot_override_other_transmission_markers(self):
        cases=[({'attempts':1},{}),
               ({'duplicate_retry_at':fields.Datetime.now()+timedelta(minutes=5),
                 'duplicate_payload_hash':'retained'},{}),
               ({},{'e_invoice_sent':True})]
        for job_values,move_values in cases:
            move=self.invoice();job=move.eta_job_ids
            job.write(dict({'state':'unknown','first_submission_candidate':True},**job_values))
            if move_values:move.write(move_values)
            client=MagicMock()
            client.request.return_value={'result':[],'metadata':{'continuationToken':'EndofResultSet'}}
            job._refresh(client)
            self.assertEqual(job.state,'unknown')
            self.assertTrue(all(c.args[0]=='GET' for c in client.request.call_args_list))

    def test_known_old_uuid_cannot_poll_away_a_new_uncertain_request(self):
        move=self.invoice();job=move.eta_job_ids
        move.write({'e_invoice_uuid':'old-known','e_invoice_status':'Invalid'})
        payload=json.dumps({'documents':[{'internalID':move.name,'issuer':{'id':self.company.vat},'totalAmount':100,'signatures':[{'value':'new'}]}]})
        job.write({'state':'unknown','payload':payload,'attempted_at':fields.Datetime.now(),'attempts':1})
        client=MagicMock()
        def response(method,path,**kwargs):
            self.assertEqual(method,'GET')
            if path.endswith('/search'):
                return {'result':[{'uuid':'old-known','internalId':move.name}],
                        'metadata':{'continuationToken':'EndofResultSet'}}
            return {'uuid':'old-known','internalId':move.name,'status':'Valid',
                    'document':{'internalID':move.name,'issuer':{'id':self.company.vat},'totalAmount':100,'signatures':[{'value':'old'}]}}
        client.request.side_effect=response
        job._refresh(client)
        self.assertEqual(job.state,'unknown');self.assertEqual(job.payload,payload)
        self.assertEqual(move.e_invoice_uuid,'old-known');self.assertEqual(move.e_invoice_status,'Invalid')
        self.assertTrue(client.request.call_args_list[0].args[1].endswith('/search'))

    def setUp(self):
        super().setUp()
        # These transport/date tests isolate the POST state machine. The real
        # final preflight is exercised end-to-end in test_historical.
        guard = patch.object(type(self.env['tradeline.eta.job']), '_preflight_submission', return_value=True)
        guard.start()
        self.addCleanup(guard.stop)

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        company_partner = {'vat': '123456789'}
        for name, value in [('company_size', 'small'), ('company_device', 1)]:
            if name in cls.env['res.partner']._fields:
                company_partner[name] = value
        cls.company.partner_id.with_context(no_vat_validation=True).write(company_partner)
        cls.env.ref('base.EGP').active = True
        cls.company.write({'client_id': 'test-client', 'client_secret': 'test-secret',
                           'config_type': 'uat', 'config_version': '1.0', 'signature_tool': 'python'})
        Account = cls.env['account.account']
        cls.receivable = Account.create({'name': 'ETA test receivable', 'code': 'ETARCV',
            'account_type': 'asset_receivable', 'reconcile': True, 'company_ids': [Command.set(cls.company.ids)]})
        cls.income = Account.create({'name': 'ETA test income', 'code': 'ETAINC',
            'account_type': 'income', 'company_ids': [Command.set(cls.company.ids)]})
        cls.partner = cls.env['res.partner'].create({'name': 'ETA test buyer',
            'property_account_receivable_id': cls.receivable.id})
        cls.journal = cls.env['account.journal'].create({'name': 'ETA test', 'code': 'ETAT',
            'type': 'sale', 'company_id': cls.company.id, 'edi_format_ids': [Command.clear()]})

    def invoice(self, invoice_date=None):
        move = self.env['account.move'].create({'move_type': 'out_invoice', 'journal_id': self.journal.id,
            'currency_id': self.env.ref('base.EGP').id,
            'partner_id': self.partner.id, 'invoice_date': invoice_date or fields.Date.today(),
            'invoice_line_ids': [Command.create({'name': 'Test item', 'account_id': self.income.id,
                'quantity': 1, 'price_unit': 100, 'tax_ids': [Command.clear()]})]})
        move.action_post()
        return move

    def test_posting_queues_without_http(self):
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Posting performed HTTP')):
            move = self.invoice()
        self.assertEqual(len(move.eta_job_ids), 1)
        self.assertEqual(move.eta_job_ids.state, 'queued')

    def invoice_user(self):
        user = new_test_user(self.env, login='eta_invoice_clerk', groups='account.group_account_invoice',
            company_id=self.company.id, company_ids=[Command.set(self.company.ids)])
        self.assertFalse(user.has_group('base.group_system'))
        self.assertFalse(user.has_group('account.group_account_manager'))
        return user

    def _cached_invoice_pdf(self, move, qr_url=False):
        attachment = self.env['ir.attachment'].create({
            'name': 'previous-invoice.pdf', 'raw': b'previous PDF bytes', 'mimetype': 'application/pdf',
            'res_model': 'account.move', 'res_id': move.id, 'res_field': 'invoice_pdf_report_file',
            'eta_invoice_qr_url': qr_url,
        })
        move.message_main_attachment_id = attachment
        move.invalidate_recordset(['invoice_pdf_report_id', 'invoice_pdf_report_file'])
        return attachment

    def test_eta_receipt_releases_pre_receipt_pdf_without_deleting_history(self):
        move = self.invoice()
        original = (move.invoice_date, move.amount_total, move.state)
        old_pdf = self._cached_invoice_pdf(move)
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Receipt application performed HTTP')):
            move.eta_job_ids._apply_remote({'uuid': 'approved-qr', 'internalId': move.name,
                'status': 'Valid', 'longId': 'authority-share-key'})
        self.assertFalse(move.invoice_pdf_report_id)
        self.assertFalse(move.message_main_attachment_id)
        self.assertTrue(old_pdf.exists())
        self.assertFalse(old_pdf.res_field)
        self.assertEqual((old_pdf.res_model, old_pdf.res_id), ('account.move', move.id))
        self.assertEqual(old_pdf.raw, b'previous PDF bytes')
        self.assertEqual((move.invoice_date, move.amount_total, move.state), original)

    def test_eta_unchanged_receipt_keeps_current_qr_pdf(self):
        move = self.invoice()
        receipt = {'uuid': 'approved-qr', 'internalId': move.name,
            'status': 'Valid', 'longId': 'authority-share-key'}
        move.eta_job_ids._apply_remote(receipt)
        current = self._cached_invoice_pdf(move, move.e_invoice_qr_code)
        move.eta_job_ids._apply_remote(receipt)
        move._eta_drop_stale_invoice_pdf()
        self.assertEqual(move.invoice_pdf_report_id, current)
        self.assertEqual(current.res_field, 'invoice_pdf_report_file')

    def test_eta_changed_share_key_releases_cached_qr_pdf(self):
        move = self.invoice()
        move.write({'e_invoice_uuid': 'approved-qr', 'e_invoice_long_id': 'old-key'})
        old_pdf = self._cached_invoice_pdf(move, move.e_invoice_qr_code)
        move.write({'e_invoice_long_id': 'new-key'})
        self.assertFalse(move.invoice_pdf_report_id)
        self.assertTrue(old_pdf.exists())
        self.assertIn('/share/new-key', move.e_invoice_qr_code)

    def test_eta_legacy_pdf_download_rebuilds_locally_once_and_stamps_rendered_qr(self):
        from odoo.addons.account.models.account_move_send import AccountMoveSend
        move = self.invoice()
        move.write({'e_invoice_uuid': 'approved-qr', 'e_invoice_long_id': 'authority-key',
                    'e_invoice_status': 'Valid'})
        old_pdf = self._cached_invoice_pdf(move)
        # Printing is an ordinary branch invoice operation, even when staff
        # cannot access the central ETA sending workspace.
        move = move.with_user(self.invoice_user())
        def render(invoices_data):
            for invoice, data in invoices_data.items():
                data['pdf_attachment_values'] = {'name': 'with-qr.pdf', 'raw': b'fresh PDF with QR',
                    'mimetype': 'application/pdf', 'res_model': 'account.move', 'res_id': invoice.id,
                    'res_field': 'invoice_pdf_report_file'}
        with patch.object(AccountMoveSend, '_prepare_invoice_pdf_report', side_effect=render) as renderer, \
                patch('requests.sessions.Session.request', side_effect=AssertionError('Printing performed HTTP')):
            result = move._get_invoice_legal_documents('pdf')
            self.assertEqual(result['content'], b'fresh PDF with QR')
            self.assertEqual(move.invoice_pdf_report_id.eta_invoice_qr_url, move.e_invoice_qr_code)
            self.assertEqual(move._get_invoice_legal_documents_all()[0]['content'], b'fresh PDF with QR')
            self.assertEqual(renderer.call_count, 1)
        self.assertFalse(old_pdf.res_field)
        self.assertTrue(old_pdf.exists())
        self.assertNotEqual(old_pdf, move.invoice_pdf_report_id)

    def test_eta_print_without_authority_share_key_keeps_existing_pdf(self):
        move = self.invoice()
        move.e_invoice_uuid = 'not-yet-shareable'
        old_pdf = self._cached_invoice_pdf(move)
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Printing performed HTTP')):
            self.assertEqual(move._get_invoice_legal_documents('pdf')['content'], b'previous PDF bytes')
        self.assertEqual(move.invoice_pdf_report_id, old_pdf)
        self.assertFalse(move.e_invoice_qr_code)

    def test_eta_send_wizard_drops_stale_pdf_before_selecting_attachments(self):
        move = self.invoice()
        move.write({'e_invoice_uuid': 'approved-qr', 'e_invoice_long_id': 'authority-key'})
        old_pdf = self._cached_invoice_pdf(move)
        widget = self.env['account.move.send']._get_default_mail_attachments_widget(
            move, self.env['mail.template'])
        self.assertFalse(move.invoice_pdf_report_id)
        self.assertNotIn(old_pdf.id, [row['id'] for row in widget])
        self.assertTrue(any(row.get('placeholder') for row in widget))

    def test_invoice_user_can_read_reset_dependencies_and_sync(self):
        user = self.invoice_user()
        move = self.invoice().with_user(user)
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Sync performed HTTP')):
            self.assertTrue(move.read(['show_reset_to_draft_button', 'eta_job_ids'])[0]['show_reset_to_draft_button'])
            self.assertEqual(move.eta_job_ids.read(['state'])[0]['state'], 'queued')
            with self.assertRaises(AccessError):
                move.action_send_electronic_invoice()
            with self.assertRaises(AccessError):
                move.action_update_electronic_invoice_status()
        menu = self.env.ref('tradeline_eta_reliability.menu_account_eta_jobs')
        self.assertNotIn(menu.id, self.env['ir.ui.menu'].with_user(user)._visible_menu_ids())

    def test_invoice_user_cannot_rewrite_transport_ledger(self):
        user = self.invoice_user()
        job = self.invoice().eta_job_ids.with_user(user)
        for operation in ('write', 'create', 'unlink'):
            self.assertFalse(job.check_access_rights(operation, raise_exception=False))
        with self.assertRaises(AccessError):
            job.write({'state': 'valid', 'move_id': job.move_id.id})
        with self.assertRaises(AccessError):
            job.read(['payload'])

    def test_invoice_user_can_read_eta_metadata_without_configuration_access(self):
        user = self.invoice_user()
        for model in ('account.tax.type.code', 'account.tax.sub.type.code', 'res.company.activity', 'eta.uom'):
            records = self.env[model].with_user(user)
            records.search([], limit=1).read(['code'])
            self.assertFalse(records.check_access_rights('write', raise_exception=False))

    def test_readonly_accountant_can_view_but_cannot_recheck(self):
        user = new_test_user(self.env, login='eta_readonly', groups='account.group_account_readonly',
            company_id=self.company.id, company_ids=[Command.set(self.company.ids)])
        job = self.invoice().eta_job_ids.with_user(user)
        self.assertEqual(job.read(['state'])[0]['state'], 'queued')
        for action in ('action_recheck', 'action_approve_current_issue_date'):
            with self.assertRaises(AccessError):
                getattr(job, action)()

    def test_invoice_user_can_recheck_and_approve_recorded_date(self):
        user = self.invoice_user()
        move = self.invoice(invoice_date=fields.Date.today() - timedelta(days=45))
        job = move.eta_job_ids
        job.write({'state': 'blocked', 'payload': 'prior-request', 'last_error': 'prior-error'})
        job.with_user(user).action_recheck()
        self.assertEqual(job.state, 'queued')
        job.with_user(user).action_approve_current_issue_date()
        self.assertEqual(job.issue_date_policy, 'current')
        self.assertEqual(job.issue_date_approved_by, user)
        self.assertNotEqual(move.invoice_date, fields.Date.today())
        self.assertTrue(self.env['ir.attachment'].search_count([
            ('res_model', '=', 'account.move'), ('res_id', '=', move.id),
            ('name', '=', 'ETA_issue_date_approval_%s.json' % job.id)]))

    def test_invoice_user_actions_enforce_invoice_record_rules_before_elevation(self):
        user = self.invoice_user()
        move = self.invoice()
        job = move.eta_job_ids
        self.env['ir.rule'].create({'name': 'ETA QA inaccessible invoice',
            'model_id': self.env['ir.model']._get_id('account.move'),
            'domain_force': "[('id', '!=', %s)]" % move.id})
        for action in ('action_recheck', 'action_approve_current_issue_date'):
            with self.assertRaises(AccessError):
                getattr(job.with_user(user), action)()
        for action in ('action_send_electronic_invoice', 'action_update_electronic_invoice_status'):
            with self.assertRaises(AccessError):
                getattr(move.with_user(user), action)()
        self.assertEqual(job.state, 'queued')
        self.assertEqual(job.issue_date_policy, 'invoice')

    def test_invoice_user_job_actions_enforce_selected_company(self):
        user = self.invoice_user()
        job = self.invoice().eta_job_ids
        other = self.env['res.company'].create({'name': 'ETA permissions other company',
            'activity_code_id': self.company.activity_code_id.id,
            'type_code_id': self.company.type_code_id.id, 'sub_type_code_id': self.company.sub_type_code_id.id})
        user.company_ids |= other
        hidden = job.with_user(user).with_context(allowed_company_ids=other.ids)
        self.assertFalse(hidden.search([('id', '=', job.id)]))
        for action in ('action_recheck', 'action_approve_current_issue_date'):
            with self.assertRaises(AccessError):
                getattr(hidden, action)()

    def _id_rule_partner(self, **values):
        details = dict(company_type='person', mobile_type='local', vat=False,
                       display_name='ID rule test buyer', country_id=SimpleNamespace(code='EG'),
                       state_id=SimpleNamespace(display_name='Cairo'), city='Cairo', street='Test street',
                       street2=False, zip=False, floor=False, room=False, landmark=False, additional_info=False)
        details.update(values)
        return SimpleNamespace(**details)

    def test_missing_national_id_is_optional_below_25000(self):
        partner = self._id_rule_partner()
        for total in (0, 24999.99):
            result = self.env['account.move']._get_eta_personal_details(partner, total)
            self.assertEqual(result['type'], 'P')
            self.assertEqual(result['id'], '')
            self.assertFalse(partner.vat)

    def test_national_id_is_required_at_inclusive_25000_boundary(self):
        for total in (25000, 49999, 50000, 50001, -25000):
            with self.assertRaisesRegex(UserError, '25,000'):
                self.env['account.move']._get_eta_personal_details(self._id_rule_partner(), total)

    def test_required_national_id_must_have_fourteen_digits(self):
        for national_id in ('1234567890123', '1234567890123A', '123456789012345'):
            with self.assertRaisesRegex(UserError, '14-digit'):
                self.env['account.move']._get_eta_personal_details(self._id_rule_partner(vat=national_id), 25000)
        partner = self._id_rule_partner(vat='12345678901234')
        self.assertEqual(self.env['account.move']._get_eta_personal_details(partner, 25000)['id'], partner.vat)

    def test_optional_recorded_id_is_not_removed_below_threshold(self):
        partner = self._id_rule_partner(vat='12345678901234')
        self.assertEqual(self.env['account.move']._get_eta_personal_details(partner, 100)['id'], partner.vat)

    def test_optional_placeholder_ids_are_omitted_without_editing_customer(self):
        for value in ('1', '00000000000000', '11111111111111', '1234567890123A'):
            partner = self._id_rule_partner(vat=value)
            for amount in (350, 1250, -3198, 24999.99):
                details = self.env['account.move']._get_eta_personal_details(partner, amount)
                self.assertEqual((details['type'], details['id']), ('P', ''))
                self.assertEqual(partner.vat, value)
            kept = self.env['account.move'].with_context(eta_keep_recorded_person_id=True)._get_eta_personal_details(partner, 350)
            self.assertEqual(kept['id'], value)
        with self.assertRaisesRegex(UserError, '14-digit'):
            self.env['account.move']._get_eta_personal_details(self._id_rule_partner(vat='1'), 25000)

    def test_optional_id_policy_preserves_recorded_reference_and_amounts(self):
        move = self.invoice(); partner = move.partner_id
        partner.with_context(no_vat_validation=True).write({'vat': '12345678901234', 'country_id': self.env.ref('base.eg').id})
        move.eta_job_ids.omit_optional_receiver_id = True
        before = (move.invoice_date, move.amount_total, partner.vat)
        with patch.object(type(partner), 'mobile_type', 'local', create=True):
            self.assertEqual(move._get_eta_personal_details(partner, 100)['id'], '')
            self.assertEqual(move.with_context(eta_keep_recorded_person_id=True)._get_eta_personal_details(partner, 100)['id'], partner.vat)
        self.assertEqual(before, (move.invoice_date, move.amount_total, partner.vat))

    def test_recorded_rejections_in_review_are_visible_without_communications_alarm(self):
        job = self.invoice().eta_job_ids; jobs = self.env['tradeline.eta.job']
        monitor = self.env['tradeline.operations.monitor'].with_context(active_test=False).search([], limit=1)
        monitor = monitor or self.env['tradeline.operations.monitor'].create({})
        self.env.ref('tradeline_eta_reliability.cron_eta_queue').write({'active': True, 'nextcall': fields.Datetime.now() + timedelta(minutes=1)})
        self.env['ir.config_parameter'].sudo().set_param('tradeline_eta_reliability.enabled_database', self.env.cr.dbname)
        self.env['ir.config_parameter'].sudo().set_param('tradeline_eta_reliability.last_processed_at', str(fields.Datetime.now()))
        messages = [
            (True, 'ETA rejected the submission (1). ' + json.dumps({'code': '1', 'details': [{'code': 'CF305', 'message': 'Receiver ID required'}]})),
            (False, json.dumps({'status': 'Invalid', 'validationSteps': [{'status': 'Invalid', 'error': {'errorCode': 'CV302', 'error': 'Item code does not exist'}}]})),
        ]
        for safe, message in messages:
            job.write({'state': 'review', 'safe_to_retry': safe, 'failures': 0, 'last_error': message})
            self.assertTrue(job.needs_attention)
            self.assertIn(job, jobs.search([('needs_attention', '=', True)]))
            self.assertIn('previous submission was rejected', job.required_action)
            self.assertTrue('customer ID' in job.required_action or 'product details' in job.required_action)
            with patch.object(type(jobs), 'search_count', side_effect=lambda domain, **kw: len(job.filtered_domain(domain))):
                codes = [s['code'] for s in monitor._probe_crons(fields.Datetime.now())]
            self.assertIn('eta:daily_rejected', codes); self.assertNotIn('eta:daily_stalled', codes)
        job.write({'safe_to_retry': False, 'last_error': 'Automatic authority-history scan is in progress.'})
        self.assertFalse(job.needs_attention)
        self.assertNotIn(job, jobs.search([('needs_attention', '=', True)]))

    def test_existing_approvals_with_duplicate_audit_are_visible_without_reclassifying_the_invoice(self):
        move = self.invoice(); normal = self.invoice().eta_job_ids; job = move.eta_job_ids
        move.write({'e_invoice_status': 'Valid', 'e_invoice_uuid': 'kept-approved'})
        before = (move.invoice_date, move.amount_total, move.e_invoice_uuid, move.e_invoice_status)
        reason = 'Multiple currently Valid ETA receipts use this invoice number; authority audit found two approvals.'
        job.write({'state': 'valid', 'last_error': reason}); normal.state = 'valid'
        self.assertTrue(job.needs_attention); self.assertFalse(normal.needs_attention)
        self.assertEqual(job.reason_summary, reason)
        self.assertIn('Finance and IT', job.required_action); self.assertIn('do not automatically cancel', job.required_action)
        jobs = self.env['tradeline.eta.job']; ids = [job.id, normal.id]
        self.assertEqual(jobs.search([('id', 'in', ids), ('needs_attention', '=', True)]), job)
        self.assertEqual(jobs.search([('id', 'in', ids), ('needs_attention', '=', False)]), normal)
        self.assertEqual(job.state, 'valid'); self.assertEqual(before, (move.invoice_date, move.amount_total, move.e_invoice_uuid, move.e_invoice_status))

    def test_duplicate_audit_refresh_preserves_payload_and_receipts_until_full_history_reconciliation(self):
        move = self.invoice(); job = move.eta_job_ids
        move.write({'e_invoice_status': 'Valid', 'e_invoice_uuid': 'kept-approved'})
        job.write({'state': 'valid', 'last_error': 'Multiple currently Valid ETA receipts use this invoice number.',
                   'payload': 'retained signed audit evidence', 'attempted_at': fields.Datetime.now()})
        before = (job.payload, job.attempted_at, move.e_invoice_uuid, move.invoice_date, move.amount_total)
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Refresh sent HTTP')):
            job.action_recheck()
        self.assertEqual(job.state, 'review'); self.assertTrue(job.historical_review)
        self.assertFalse(job.historical_submission_verified); self.assertTrue(job.needs_attention)
        self.assertEqual(before, (job.payload, job.attempted_at, move.e_invoice_uuid, move.invoice_date, move.amount_total))

    def test_known_duplicate_approval_review_is_not_a_scheduler_or_uncertain_submission_alarm(self):
        job = self.invoice().eta_job_ids; jobs = self.env['tradeline.eta.job']
        job.write({'state': 'review', 'last_error': 'Multiple currently Valid ETA receipts use this invoice number.', 'failures': 0})
        monitor = self.env['tradeline.operations.monitor'].with_context(active_test=False).search([], limit=1)
        monitor = monitor or self.env['tradeline.operations.monitor'].create({})
        self.env.ref('tradeline_eta_reliability.cron_eta_queue').write({'active': True, 'failure_count': 0, 'nextcall': fields.Datetime.now() + timedelta(minutes=1)})
        self.env['ir.config_parameter'].sudo().set_param('tradeline_eta_reliability.enabled_database', self.env.cr.dbname)
        self.env['ir.config_parameter'].sudo().set_param('tradeline_eta_reliability.last_processed_at', str(fields.Datetime.now()))
        with patch.object(type(jobs), 'search_count', side_effect=lambda domain, **kw: len(job.filtered_domain(domain))):
            codes = {s['code'] for s in monitor._probe_crons(fields.Datetime.now())}
        self.assertTrue(job.needs_attention)
        self.assertFalse(codes.intersection({'eta:daily_stalled', 'eta:daily_progress', 'eta:scheduler'}))

    def test_national_id_rule_keeps_foreign_and_business_identity_rules(self):
        foreign = self._id_rule_partner(mobile_type='foreigner', vat='TEST-PASSPORT',
                                        country_id=SimpleNamespace(code='US'))
        result = self.env['account.move']._get_eta_personal_details(foreign, 60000)
        self.assertEqual((result['type'], result['id']), ('F', 'TEST-PASSPORT'))
        with self.assertRaisesRegex(UserError, 'Tax ID'):
            self.env['account.move']._get_eta_personal_details(self._id_rule_partner(company_type='company'), 100)

    def test_national_id_threshold_uses_posted_total_after_discount(self):
        move = self.invoice()
        # Legacy order_total was the gross line price before discounts.
        partner = move.partner_id
        with patch.object(type(partner), 'mobile_type', 'local', create=True), \
                patch.object(type(self.env['res.currency']), '_convert', return_value=100):
            result = move._get_eta_personal_details(partner, 60000)
        self.assertEqual(result['type'], 'P')
        self.assertEqual(result['id'], '')

    def test_historical_backlog_does_not_starve_new_submissions_or_statuses(self):
        for state in ('queued', 'accepted', 'unknown'):
            self.invoice().eta_job_ids.write({'state': state, 'next_attempt': False})
        due = self.env['tradeline.eta.job']._due_jobs('all')
        self.assertEqual([job.state for job in due[:3]], ['queued', 'accepted', 'unknown'])

    def test_fresh_and_old_queued_invoices_both_progress_during_recovery(self):
        self.env['tradeline.eta.job'].sudo().search([('state', '=', 'queued')]).write({
            'next_attempt': fields.Datetime.now() + timedelta(days=1)})
        older = self.invoice().eta_job_ids
        fresh = self.invoice().eta_job_ids
        self.env.cr.execute('UPDATE tradeline_eta_job SET create_date=%s WHERE id=%s',
                            [fields.Datetime.now() - timedelta(days=2), older.id])
        due = self.env['tradeline.eta.job']._due_jobs('submit')
        self.assertEqual([job for job in due if job.state == 'queued'][:2], [fresh, older])
        self.assertEqual(sum(job.id == fresh.id for job in due), 1)
        self.assertEqual(sum(job.id == older.id for job in due), 1)

    def test_monitor_detects_wrong_database_activation(self):
        monitor = self.env['tradeline.operations.monitor'].with_context(active_test=False).search([], limit=1)
        if not monitor:
            monitor = self.env['tradeline.operations.monitor'].create({})
        self.env.ref('tradeline_eta_reliability.cron_eta_queue').active = True
        self.env['ir.config_parameter'].sudo().set_param('tradeline_eta_reliability.enabled_database', 'different-database')
        self.assertIn('eta:scheduler', [s['code'] for s in monitor._probe_crons(fields.Datetime.now())])

    def test_monitor_detects_a_queue_that_never_started(self):
        job = self.invoice().eta_job_ids
        self.env.cr.execute('UPDATE tradeline_eta_job SET create_date=%s WHERE id=%s',
                            [fields.Datetime.now() - timedelta(minutes=30), job.id])
        monitor = self.env['tradeline.operations.monitor'].with_context(active_test=False).search([], limit=1)
        if not monitor:
            monitor = self.env['tradeline.operations.monitor'].create({})
        self.assertEqual(job.failures, 0)
        self.assertIn('eta:daily_stalled', [s['code'] for s in monitor._probe_crons(fields.Datetime.now())])

    def test_eta_alert_resolves_after_scheduler_recovers_but_not_after_failed_probe(self):
        monitor = self.env['tradeline.operations.monitor'].with_context(active_test=False).search([], limit=1)
        if not monitor:
            monitor = self.env['tradeline.operations.monitor'].create({})
        now = fields.Datetime.now()
        self.env.ref('tradeline_eta_reliability.cron_eta_queue').active = True
        self.env['ir.config_parameter'].sudo().set_param(
            'tradeline_eta_reliability.enabled_database', 'different-database')
        observations = [s for s in monitor._probe_crons(now) if s['code'] == 'eta:scheduler']
        monitor._reconcile(observations, {'crons'}, now)
        incident = monitor.incident_ids.filtered(lambda i: i.code == 'eta:scheduler')
        self.assertTrue(incident)
        monitor._reconcile([], set(), now)
        self.assertNotEqual(incident.state, 'resolved')
        self.env['ir.config_parameter'].sudo().set_param(
            'tradeline_eta_reliability.enabled_database', self.env.cr.dbname)
        self.env.ref('tradeline_eta_reliability.cron_eta_queue').write({'nextcall': now, 'failure_count': 0})
        self.assertFalse([s for s in monitor._probe_crons(now) if s['code'] == 'eta:scheduler'])
        monitor._reconcile([], {'crons'}, now)
        self.assertEqual(incident.state, 'resolved')

    def test_signer_does_not_commit_and_has_timeout(self):
        move = self.invoice()
        move.e_invoice_canonical = 'canonical'
        with patch('odoo.addons.tradeline_eta_reliability.models.account_move.requests.get',
                   return_value=SimpleNamespace(status_code=200, text='signed')) as http, \
                patch.object(type(move), '_eta_validate_signature_digest', return_value=True):
            with patch.object(type(self.env.cr), 'commit', side_effect=AssertionError('Signer committed')):
                self.assertEqual(move.get_signature_value(), 'signed')
        self.assertEqual(http.call_args.kwargs['timeout'], (5, 25))

    def test_known_uuid_is_never_resubmitted(self):
        move = self.invoice()
        move.write({'e_invoice_uuid': 'known-uuid', 'e_invoice_status': 'Submitted'})
        client = MagicMock()
        client.request.return_value = {'uuid': 'known-uuid', 'internalId': move.name, 'status': 'Valid'}
        move.eta_job_ids._submit(client)
        self.assertEqual(client.request.call_args.args[0], 'GET')
        self.assertEqual(move.eta_job_ids.state, 'valid')

    def test_uncertain_submission_is_not_resent(self):
        move = self.invoice()
        job = move.eta_job_ids
        job.write({'state': 'unknown', 'attempted_at': fields.Datetime.now(),
                   'payload': json.dumps({'documents': [{'internalID': move.name, 'dateTimeIssued': str(move.invoice_date) + 'T00:00:00Z'}]})})
        client = MagicMock()
        client.request.return_value = {'result': [], 'metadata': {'continuationToken': 'EndofResultSet'}}
        job._refresh(client)
        self.assertEqual(job.state, 'unknown')
        self.assertTrue(all(c.args[0] == 'GET' for c in client.request.call_args_list))
        self.assertFalse(move.e_invoice_uuid)

    def test_unattempted_legacy_document_requires_authoritative_absence(self):
        move = self.invoice()
        job = move.eta_job_ids
        job.write({'state': 'unknown', 'first_submission_candidate': True})
        client = MagicMock()
        client.request.return_value = {'result': [], 'metadata': {'continuationToken': 'EndofResultSet'}}
        job._refresh(client)
        self.assertEqual(job.state, 'queued')
        self.assertTrue(all(c.args[0] == 'GET' for c in client.request.call_args_list))

    def test_lost_response_is_not_treated_as_an_unattempted_document(self):
        move = self.invoice()
        job = move.eta_job_ids
        job.write({'state': 'unknown', 'first_submission_candidate': True,
                   'attempted_at': fields.Datetime.now()})
        client = MagicMock()
        client.request.return_value = {'result': [], 'metadata': {'continuationToken': 'EndofResultSet'}}
        job._refresh(client)
        self.assertEqual(job.state, 'unknown')

    def test_unknown_does_not_match_reused_internal_id(self):
        move = self.invoice()
        job = move.eta_job_ids
        job.write({'state': 'unknown', 'attempted_at': fields.Datetime.now(),
                   'payload': json.dumps({'documents': [{'internalID': move.name, 'totalAmount': 100}]})})
        client = MagicMock()
        client.request.side_effect = [
            {'result': [{'uuid': 'other', 'internalId': move.name}]},
            {'uuid': 'other', 'internalId': move.name, 'status': 'Valid',
             'document': json.dumps({'internalID': move.name, 'totalAmount': 200})}]
        job._refresh(client)
        self.assertEqual(job.state, 'unknown')
        self.assertFalse(move.e_invoice_uuid)

    def test_wrong_identity_cannot_mark_valid(self):
        move = self.invoice()
        with self.assertRaises(EtaError):
            move.eta_job_ids._apply_remote({'uuid': 'wrong', 'internalId': 'different-invoice', 'status': 'Valid'})
        self.assertFalse(move.e_invoice_uuid)

    def test_native_dispatcher_does_not_send_a_second_copy(self):
        move = self.invoice()
        eta = self.env.ref('l10n_eg_edi_eta.edi_eg_eta')
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Native dispatcher sent HTTP')):
            result = eta._l10n_eg_edi_post_invoice(move)
        self.assertEqual(result[move]['blocking_level'], 'info')
        self.assertEqual(len(move.eta_job_ids), 1)

    def test_verified_valid_status_clears_only_matching_native_error(self):
        move = self.invoice()
        eta = self.env.ref('l10n_eg_edi_eta.edi_eg_eta')
        native = self.env['account.edi.document'].create({'move_id': move.id, 'edi_format_id': eta.id,
            'state': 'to_send', 'error': 'Missing native signing payload', 'blocking_level': 'error'})
        move.eta_job_ids._apply_remote({'uuid': 'verified-uuid', 'internalId': move.name, 'status': 'Valid',
            'submissionUUID': 'verified-submission', 'document': json.dumps({'internalID': move.name,
                'issuer': {'id': '123456789'}, 'dateTimeIssued': str(move.invoice_date) + 'T00:00:00Z'})})
        self.assertEqual(native.state, 'sent')
        self.assertFalse(native.error)
        self.assertEqual(move.l10n_eg_uuid, 'verified-uuid')

    def test_invalid_remains_visible_and_not_queued(self):
        move = self.invoice()
        move.eta_job_ids._apply_remote({'uuid': 'eta-uuid', 'internalId': move.name, 'status': 'Invalid'})
        self.assertEqual(move.e_invoice_status, 'Invalid')
        self.assertEqual(move.eta_job_ids.state, 'invalid')
        self.assertTrue(move.eta_job_ids.last_error)

    def test_async_rejection_fetches_field_details_before_becoming_terminal(self):
        move = self.invoice()
        move.e_invoice_uuid = 'eta-invalid'
        job = move.eta_job_ids
        job.submission_uuid = 'eta-submission'
        client = MagicMock()
        client.request.side_effect = [
            {'documentSummary': [{'uuid': 'eta-invalid', 'internalId': move.name, 'status': 'Invalid'}]},
            {'uuid': 'eta-invalid', 'internalId': move.name, 'status': 'Invalid',
             'validationResults': {'errorCode': 'NI310', 'propertyName': 'receiverId', 'error': 'Invalid customer ID'}}]
        job._refresh(client)
        self.assertEqual(job.state, 'invalid')
        self.assertIn('NI310', job.last_error)
        self.assertIn('receiverId', move.e_invoice_issue)
        self.assertEqual([c.args[0] for c in client.request.call_args_list], ['GET', 'GET'])

    def _known_accepted_receipt(self):
        move = self.invoice()
        move.write({'e_invoice_uuid': 'known-accepted-uuid', 'e_invoice_status': 'Submitted',
                    'e_invoice_sent': True, 'eta_submission_pending': True})
        job = move.eta_job_ids
        payload = json.dumps({'documents': [{'internalID': move.name, 'signatures': [{'value': 'retained'}]}]})
        job.write({'state': 'accepted', 'submission_uuid': 'known-submission', 'attempts': 1,
                   'attempted_at': fields.Datetime.now(), 'payload': payload,
                   'payload_hash': hashlib.sha256(payload.encode()).hexdigest()})
        return move, job

    def _known_receipt_result(self, move, status='Valid'):
        return {'uuid': move.e_invoice_uuid, 'internalId': move.name, 'status': status,
                'document': {'internalID': move.name, 'issuer': {'id': self.company.vat},
                             'taxTotals': [{'taxType': 'T1', 'amount': 0}]}}

    def test_accepted_known_receipt_without_archive_polls_raw_once_and_retains_request(self):
        move, job = self._known_accepted_receipt()
        eta = self.env.ref('l10n_eg_edi_eta.edi_eg_eta')
        native = self.env['account.edi.document'].create({'move_id': move.id, 'edi_format_id': eta.id,
            'state': 'to_send', 'error': 'Waiting for authority receipt', 'blocking_level': 'info'})
        protected = job.read(['attempts', 'attempted_at', 'payload', 'payload_hash', 'submission_uuid'])[0]
        financial = move._eta_local_vat_financial_snapshot()
        client = MagicMock()
        client.request.return_value = self._known_receipt_result(move)
        with patch.object(type(job), '_submit', side_effect=AssertionError('Receipt polling submitted a document')):
            job._refresh(client)
        client.request.assert_called_once_with('GET', '/api/v1.0/documents/known-accepted-uuid/raw')
        self.assertEqual(job.state, 'valid')
        self.assertEqual(job.read(list(protected))[0], protected)
        self.assertEqual(move._eta_local_vat_financial_snapshot(), financial)
        self.assertTrue(move.l10n_eg_eta_json_doc_id)
        self.assertEqual(json.loads(move.l10n_eg_eta_json_doc_id.raw)['request'], client.request.return_value['document'])
        self.assertEqual(native.state, 'sent')
        self.assertEqual(move.l10n_eg_uuid, 'known-accepted-uuid')

    def test_direct_known_raw_receipt_identity_mismatch_cannot_change_accepted_invoice(self):
        for mismatch in ('uuid', 'invoice', 'issuer'):
            with self.subTest(mismatch=mismatch):
                move, job = self._known_accepted_receipt()
                response = self._known_receipt_result(move)
                if mismatch == 'uuid': response['uuid'] = 'another-document'
                elif mismatch == 'invoice': response['internalId'] = 'another-invoice'
                else: response['document']['issuer']['id'] = '999999999'
                before = (move._eta_local_vat_financial_snapshot(),
                    job.read(['state','attempts','attempted_at','payload','payload_hash','submission_uuid'])[0])
                client = MagicMock(); client.request.return_value = response
                with self.assertRaises(UserError if mismatch == 'issuer' else EtaError):
                    job._refresh(client)
                self.assertEqual((move._eta_local_vat_financial_snapshot(),
                    job.read(['state','attempts','attempted_at','payload','payload_hash','submission_uuid'])[0]), before)
                self.assertFalse(move.l10n_eg_eta_json_doc_id)
                client.request.assert_called_once_with('GET', '/api/v1.0/documents/known-accepted-uuid/raw')

    def test_known_raw_failure_never_retries_posts_or_discards_retained_attempt(self):
        failures = [requests.ConnectionError('Remote closed the connection'), requests.ReadTimeout('Read timed out')]
        failures += [EtaError('ETA returned HTTP %s.' % code, status=code) for code in (401, 404, 429, 503)]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__, status=getattr(failure,'status',None)):
                move, job = self._known_accepted_receipt()
                before = job.read(['state','next_attempt','attempts','attempted_at','payload','payload_hash','submission_uuid'])[0]
                financial = move._eta_local_vat_financial_snapshot()
                client = MagicMock(); client.request.side_effect = failure
                with self.assertRaises(type(failure)):
                    job._refresh(client)
                client.request.assert_called_once_with('GET', '/api/v1.0/documents/known-accepted-uuid/raw')
                self.assertEqual(job.read(list(before))[0], before)
                self.assertEqual(move._eta_local_vat_financial_snapshot(), financial)
                self.assertEqual(move.e_invoice_uuid, 'known-accepted-uuid')

    def test_cached_known_receipt_keeps_existing_submission_summary_poll(self):
        move, job = self._known_accepted_receipt()
        job._apply_remote(self._known_receipt_result(move))
        job.state = 'accepted'
        client = MagicMock()
        client.request.return_value = {'documentSummary': [{'uuid':move.e_invoice_uuid,
            'internalId':move.name,'status':'Valid'}]}
        job._refresh(client)
        client.request.assert_called_once_with('GET', '/api/v1.0/documentsubmissions/known-submission',
                                               params={'pageNo':1,'pageSize':100})
        self.assertEqual(job.state, 'valid')

    def test_old_invoice_is_blocked_without_rewriting_date(self):
        old = fields.Date.today() - timedelta(days=30)
        move = self.invoice(invoice_date=old)
        with self.assertRaises(UserError):
            move.eta_job_ids._prepare()
        self.assertEqual(move.invoice_date, old)

    def test_generated_document_and_canonical_use_invoice_date_instead_of_submission_day(self):
        issued = date(2026, 7, 15)
        move = self.invoice(invoice_date=issued)
        with patch.object(type(move), '_get_eta_invoice_lines', return_value=([], 0, 100, 100)), \
                patch.object(type(move), '_get_eta_personal_details', side_effect=lambda *a: {'address': {}}), \
                patch.object(type(move), '_get_eta_tax_totals', return_value=[]), \
                patch.object(type(move), 'inv_type', '', create=True), \
                patch.object(type(move), 'reference_number', '', create=True), \
                patch('requests.sessions.Session.request', side_effect=AssertionError('Generation performed HTTP')):
            document, _environment = move.action_generate_eta_json()
        expected = '2026-07-14T21:00:00Z'
        self.assertEqual(document['dateTimeIssued'], expected)
        self.assertIn('"DATETIMEISSUED""%s"' % expected, move.e_invoice_canonical)
        self.assertEqual(move.invoice_date, issued)

    def test_cairo_issue_dates_cover_winter_summer_and_clock_changes(self):
        for issued, expected in (
                (date(2026, 1, 15), '2026-01-14T22:00:00Z'),
                (date(2026, 7, 15), '2026-07-14T21:00:00Z'),
                (date(2026, 4, 24), '2026-04-23T22:00:00Z'),
                (date(2026, 10, 30), '2026-10-29T22:00:00Z')):
            move = self.invoice(invoice_date=issued)
            # Exercise the midnight fallback even when the test runs on one
            # of these dates; matching stored issue-time evidence is separate.
            self.env.cr.execute('UPDATE account_move SET create_date=%s WHERE id=%s',
                                [fields.Datetime.to_datetime('2025-01-01 00:00:00'), move.id])
            move.invalidate_recordset(['create_date'])
            self.assertEqual(move._eta_issue_timestamp(), expected)

    def test_valid_cairo_date_can_have_previous_utc_date(self):
        move = self.invoice(invoice_date=date(2026, 7, 15))
        job = move.eta_job_ids
        job.payload = json.dumps({'documents': [{'internalID': move.name,
            'dateTimeIssued': '2026-07-14T21:00:00Z'}]})
        client = MagicMock()
        client.request.return_value = {'acceptedDocuments': [{'uuid': 'cairo-date', 'internalId': move.name}]}
        with patch.object(self.env.cr, 'commit'):
            job._submit(client)
        self.assertEqual(job.state, 'accepted')
        self.assertEqual(client.request.call_count, 1)

    def test_stored_creation_time_is_retained_only_on_the_invoice_cairo_date(self):
        move = self.invoice(invoice_date=date(2026, 7, 15))
        for created, expected in (
                ('2026-07-14 21:30:00', '2026-07-14T21:30:00Z'),
                ('2026-07-15 18:45:00', '2026-07-15T18:45:00Z'),
                ('2026-07-16 12:00:00', '2026-07-14T21:00:00Z')):
            self.env.cr.execute('UPDATE account_move SET create_date=%s WHERE id=%s', [created, move.id])
            move.invalidate_recordset(['create_date'])
            self.assertEqual(move._eta_issue_timestamp(), expected)

    def test_previously_prepared_wrong_issue_date_cannot_be_transmitted(self):
        move = self.invoice(invoice_date=fields.Date.today() - timedelta(days=1))
        job = move.eta_job_ids
        job.payload = json.dumps({'documents': [{'internalID': move.name,
            'dateTimeIssued': str(fields.Date.today()) + 'T00:00:00Z'}]})
        client = MagicMock()
        with self.assertRaises(UserError):
            job._submit(client)
        client.request.assert_not_called()
        self.assertFalse(job.attempted_at)
        self.assertEqual(job.attempts, 0)

    def test_ambiguous_post_marker_survives_transport_failure(self):
        move = self.invoice()
        job = move.eta_job_ids
        job.payload = json.dumps({'documents': [{'internalID': move.name, 'dateTimeIssued': str(move.invoice_date) + 'T00:00:00Z'}]})
        client = MagicMock()
        client.request.side_effect = requests.Timeout()
        with patch.object(self.env.cr, 'commit') as commit:
            try:
                job._submit(client)
            except requests.Timeout:
                pass
            else:
                self.fail('The simulated HTTP timeout did not occur')
        self.assertTrue(commit.called)
        self.assertEqual(job.state, 'unknown')
        self.assertTrue(job.attempted_at)

    def test_throttled_post_can_retry_without_being_lost(self):
        move = self.invoice()
        job = move.eta_job_ids
        job.payload = json.dumps({'documents': [{'internalID': move.name, 'dateTimeIssued': str(move.invoice_date) + 'T00:00:00Z'}]})
        client = MagicMock()
        client.request.side_effect = EtaError('ETA returned HTTP 429.', 429, 120)
        with patch.object(self.env.cr, 'commit'):
            job._submit(client)
        self.assertEqual(job.state, 'queued')
        self.assertTrue(job.next_attempt)
        self.assertEqual(job.failures, 1)

    def _uncertain_request(self, exc):
        move = self.invoice()
        job = move.eta_job_ids
        payload = json.dumps({'documents': [{'internalID': move.name,
            'dateTimeIssued': str(move.invoice_date) + 'T00:00:00Z'}]})
        job.write({'payload': payload, 'payload_hash': hashlib.sha256(payload.encode()).hexdigest()})
        client = MagicMock(); client.request.side_effect = exc
        # TransactionCase.assertRaises adds an automatic rollback. The real
        # worker's pre-POST commit survives, so retain this simulated marker.
        with patch.object(self.env.cr, 'commit'):
            try:
                job._submit(client)
            except type(exc) as caught:
                self.assertIs(caught, exc)
            else:
                self.fail('The simulated request did not fail')
        self.assertEqual(client.request.call_count, 1)
        self.assertEqual(client.request.call_args.args[0], 'POST')
        return job

    def _uncertain_evidence(self, job):
        return self.env['ir.attachment'].sudo().search([
            ('res_model', '=', job._name), ('res_id', '=', job.id),
            ('name', '=like', 'ETA_uncertain_request_failure_%')], order='id')

    def test_uncertain_http_failure_is_private_and_survives_get_only_reconciliation(self):
        exc = EtaError('private response body and customer ID', 422, code='RequestRejected')
        job = self._uncertain_request(exc)
        before = (job.payload, job.payload_hash, job.attempted_at, job.attempts,
                  job.move_id.invoice_date, job.move_id.amount_total, job.safe_to_retry)
        self.assertTrue(job._preserve_uncertain_request_failure(exc))
        record = self._uncertain_evidence(job)
        self.assertEqual(len(record), 1)
        self.assertFalse(record.public)
        self.assertEqual(record.company_id, job.company_id)
        data = json.loads(record.raw)
        self.assertEqual((data['http_status'], data['error_code']), (422, 'RequestRejected'))
        self.assertFalse(data['delivery_proven']); self.assertFalse(data['retry_authorized'])
        client = MagicMock()
        client.request.return_value = {'result': [], 'metadata': {'continuationToken': 'EndofResultSet'}}
        job._refresh(client)
        self.assertEqual(job.state, 'unknown')
        self.assertTrue(all(c.args[0] == 'GET' for c in client.request.call_args_list))
        self.assertEqual(before, (job.payload, job.payload_hash, job.attempted_at, job.attempts,
            job.move_id.invoice_date, job.move_id.amount_total, job.safe_to_retry))
        self.assertEqual(json.loads(record.raw), data)
        self.assertNotIn('private response', record.raw.decode())

    def test_uncertain_timeout_records_type_without_message_or_claiming_http_status(self):
        exc = requests.Timeout('secret URL, token and request body')
        job = self._uncertain_request(exc)
        self.assertTrue(job._preserve_uncertain_request_failure(exc))
        data = json.loads(self._uncertain_evidence(job).raw)
        self.assertEqual(data['exception_type'], 'Timeout')
        self.assertIsNone(data['http_status']); self.assertIsNone(data['error_code'])
        self.assertNotIn('secret', json.dumps(data))
        self.assertEqual((job.state, job.attempts), ('unknown', 1))

    def test_untagged_reconciliation_error_cannot_fabricate_original_failure_evidence(self):
        job = self.invoice().eta_job_ids
        job.write({'state': 'unknown', 'attempts': 1, 'attempted_at': fields.Datetime.now(), 'payload': 'kept'})
        self.assertFalse(job._preserve_uncertain_request_failure(EtaError('later GET failed', 503)))
        self.assertFalse(self._uncertain_evidence(job))

    def test_uncertain_evidence_rejects_wrong_job_attempt_time_and_retained_request(self):
        exc = EtaError('failure', 503); job = self._uncertain_request(exc)
        original = dict(exc.eta_submission_failure)
        for key, value in [('job', job.id + 1), ('attempts', 2), ('attempted_at', '2000-01-01 00:00:00'),
                           ('payload_sha256', 'different-request')]:
            exc.eta_submission_failure = dict(original, **{key: value})
            self.assertFalse(job._preserve_uncertain_request_failure(exc))
        self.assertFalse(self._uncertain_evidence(job))
        exc.eta_submission_failure = original
        job.state = 'queued'
        self.assertFalse(job._preserve_uncertain_request_failure(exc))

    def test_first_uncertain_request_failure_is_not_overwritten(self):
        exc = EtaError('first', 422, code='FirstCode'); job = self._uncertain_request(exc)
        self.assertTrue(job._preserve_uncertain_request_failure(exc))
        record = self._uncertain_evidence(job); before = record.raw
        later = EtaError('later', 503, code='DifferentCode')
        later.eta_submission_failure = dict(exc.eta_submission_failure)
        self.assertFalse(job._preserve_uncertain_request_failure(later))
        self.assertEqual(self._uncertain_evidence(job), record)
        self.assertEqual(record.raw, before)

    def test_another_attempt_has_separate_diagnostic_without_authorizing_it(self):
        exc = EtaError('first', 422); job = self._uncertain_request(exc)
        self.assertTrue(job._preserve_uncertain_request_failure(exc))
        first = self._uncertain_evidence(job).raw
        job.write({'attempts': 2, 'attempted_at': job.attempted_at + timedelta(minutes=10)})
        exc.eta_submission_failure.update(attempts=2, attempted_at=fields.Datetime.to_string(job.attempted_at))
        self.assertTrue(job._preserve_uncertain_request_failure(exc))
        records = self._uncertain_evidence(job)
        self.assertEqual(len(records), 2); self.assertEqual(records[0].raw, first)
        self.assertTrue(all(not json.loads(record.raw)['retry_authorized'] for record in records))
        self.assertFalse(job.safe_to_retry)

    def test_diagnostic_storage_failure_cannot_replace_uncertainty_or_leak_details(self):
        exc = requests.Timeout('private HTTP details'); job = self._uncertain_request(exc)
        before = (job.payload, job.payload_hash, job.attempted_at, job.attempts)
        with patch.object(type(self.env['ir.attachment']), 'create', side_effect=ValidationError('private storage details')), \
                self.assertLogs('odoo.addons.tradeline_eta_reliability.models.eta_job', level='WARNING') as logs:
            self.assertFalse(job._preserve_uncertain_request_failure(exc))
        self.assertNotIn('private', '\n'.join(logs.output))
        job._delay('Submission outcome needs reconciliation.', seconds=900, state='unknown')
        self.assertEqual(job.state, 'unknown'); self.assertTrue(job.next_attempt)
        self.assertEqual(before, (job.payload, job.payload_hash, job.attempted_at, job.attempts))
        self.assertFalse(self._uncertain_evidence(job))

    def test_uncertain_evidence_sanitizes_external_error_code_and_status(self):
        exc = EtaError('private message', 999, code='unsafe code contains a token')
        job = self._uncertain_request(exc)
        self.assertTrue(job._preserve_uncertain_request_failure(exc))
        data = json.loads(self._uncertain_evidence(job).raw)
        self.assertIsNone(data['http_status']); self.assertIsNone(data['error_code'])
        self.assertNotIn('private', json.dumps(data)); self.assertNotIn('token', json.dumps(data))

    def test_accepted_post_is_saved_with_uuid(self):
        move = self.invoice()
        job = move.eta_job_ids
        job.payload = json.dumps({'documents': [{'internalID': move.name, 'dateTimeIssued': str(move.invoice_date) + 'T00:00:00Z'}]})
        client = MagicMock()
        client.request.return_value = {'submissionUUID': 'submission-1',
            'acceptedDocuments': [{'uuid': 'accepted-1', 'internalId': move.name, 'longId': 'long-1'}],
            'rejectedDocuments': []}
        with patch.object(self.env.cr, 'commit'):
            job._submit(client)
        self.assertEqual(job.state, 'accepted')
        self.assertEqual(move.e_invoice_uuid, 'accepted-1')
        self.assertEqual(job.submission_uuid, 'submission-1')
        self.assertFalse(move.eta_submission_pending)
        self.assertEqual(move.e_invoice_date, job.attempted_at)

    def test_submission_validation_details_remain_actionable_without_automatic_resend(self):
        move = self.invoice()
        job = move.eta_job_ids
        job.payload = json.dumps({'documents': [{'internalID': move.name, 'dateTimeIssued': str(move.invoice_date) + 'T00:00:00Z'}]})
        client = MagicMock()
        client.request.return_value = {'acceptedDocuments': [], 'rejectedDocuments': [{
            'internalId': move.name, 'error': {'code': '1', 'message': 'Validation failed',
                'details': [{'target': 'itemCode', 'message': 'Code is not approved'}]}}]}
        with patch.object(self.env.cr, 'commit'):
            job._submit(client)
        self.assertEqual(job.state, 'blocked')
        self.assertIn('itemCode', job.last_error)
        self.assertIn('Code is not approved', move.e_invoice_issue)
        self.assertNotIn(job, self.env['tradeline.eta.job']._due_jobs('all'))
        self.assertEqual(move.amount_total, 100)

    def test_corrected_invalid_document_queues_without_posting_or_losing_history(self):
        move = self.invoice()
        move.write({'e_invoice_uuid': 'invalid-old', 'e_invoice_status': 'Invalid', 'e_invoice_sent': True})
        client = MagicMock()
        client.request.return_value = {'uuid': 'invalid-old', 'internalId': move.name, 'status': 'Invalid',
            'document': {'internalID': move.name, 'receiver': {'id': 'old-id'}, 'totalAmount': 100}}
        corrected = {'internalID': move.name, 'receiver': {'id': 'corrected-id'}, 'totalAmount': 100}
        with patch('odoo.addons.tradeline_eta_reliability.models.account_move.EtaClient', return_value=client):
            with patch.object(type(move), '_eta_correction_document', return_value=corrected):
                move.action_send_electronic_invoice()
        self.assertFalse(move.e_invoice_uuid)
        self.assertEqual(move.eta_job_ids.state, 'queued')
        self.assertTrue(all(c.args[0] == 'GET' for c in client.request.call_args_list))
        self.assertTrue(self.env['ir.attachment'].search_count([
            ('res_model', '=', 'account.move'), ('res_id', '=', move.id), ('name', '=', 'ETA_rejected_invalid-old.json')]))
        self.assertEqual(move.amount_total, 100)

    def test_terminal_rejection_cannot_release_a_different_or_unverifiable_attempt(self):
        for retained in ('different', 'missing', 'bad-hash'):
            with self.subTest(retained=retained):
                move, job = self._known_accepted_receipt()
                response = self._known_receipt_result(move, status='Invalid')
                job._apply_remote(response)
                payload = json.dumps({'documents': [response['document']]})
                if retained == 'different':
                    payload = json.dumps({'documents': [dict(response['document'], signatures=[{'value':'newer'}])]})
                job.write({'payload': payload if retained != 'missing' else False,
                    'payload_hash': hashlib.sha256(payload.encode()).hexdigest() if retained != 'bad-hash' else 'bad'})
                before = job.read(['state','payload','payload_hash','attempts','attempted_at','submission_uuid'])[0]
                client = MagicMock(); client.request.return_value = response
                with patch('odoo.addons.tradeline_eta_reliability.models.account_move.EtaClient', return_value=client), \
                        self.assertRaisesRegex(UserError, 'retained submission'):
                    move._eta_queue_corrected_document()
                self.assertEqual(job.read(list(before))[0], before)
                self.assertEqual(move.e_invoice_uuid, 'known-accepted-uuid')
                self.assertEqual([call.args[0] for call in client.request.call_args_list], ['GET'])

    def test_unchanged_invalid_document_cannot_be_resent(self):
        move = self.invoice()
        move.write({'e_invoice_uuid': 'invalid-old', 'e_invoice_status': 'Invalid', 'e_invoice_sent': True})
        client = MagicMock()
        original = {'internalID': move.name, 'totalAmount': 100, 'dateTimeIssued': '2026-01-01T00:00:00Z'}
        client.request.return_value = {'uuid': 'invalid-old', 'internalId': move.name, 'status': 'Invalid', 'document': original}
        with patch('odoo.addons.tradeline_eta_reliability.models.account_move.EtaClient', return_value=client):
            with patch.object(type(move), '_eta_correction_document', return_value=dict(original, dateTimeIssued='2026-10-28T00:00:00Z')):
                with self.assertRaises(UserError):
                    move.action_send_electronic_invoice()
        self.assertEqual(move.e_invoice_uuid, 'invalid-old')
        self.assertTrue(all(c.args[0] == 'GET' for c in client.request.call_args_list))

    def test_duplicate_rejection_requires_original_receipt_reconciliation_before_correction(self):
        move = self.invoice()
        move.write({'e_invoice_uuid': 'duplicate-old', 'e_invoice_status': 'Invalid'})
        client = MagicMock()
        client.request.return_value = {'uuid': 'duplicate-old', 'internalId': move.name,
            'status': 'Invalid', 'validationResults': {'errorCode': 'DS301'},
            'document': {'internalID': move.name, 'receiver': {'id': 'old'}}}
        with patch('odoo.addons.tradeline_eta_reliability.models.account_move.EtaClient', return_value=client), \
                patch.object(type(move), '_eta_correction_document', return_value={
                    'internalID': move.name, 'receiver': {'id': 'changed'}}):
            with self.assertRaisesRegex(UserError, 'duplicate'):
                move.action_send_electronic_invoice()
        self.assertEqual(move.e_invoice_uuid, 'duplicate-old')
        self.assertEqual([c.args[0] for c in client.request.call_args_list], ['GET'])

    def test_previously_invalid_but_now_valid_document_is_refreshed_only(self):
        move = self.invoice()
        move.write({'e_invoice_uuid': 'same-uuid', 'e_invoice_status': 'Invalid'})
        client = MagicMock()
        client.request.return_value = {'uuid': 'same-uuid', 'internalId': move.name, 'status': 'Valid'}
        with patch('odoo.addons.tradeline_eta_reliability.models.account_move.EtaClient', return_value=client):
            move.action_send_electronic_invoice()
        self.assertEqual(move.e_invoice_uuid, 'same-uuid')
        self.assertEqual(move.e_invoice_status, 'Valid')
        self.assertTrue(all(c.args[0] == 'GET' for c in client.request.call_args_list))

    def test_timeout_and_throttling_are_bounded(self):
        client = EtaClient(self.company)
        client.token = 'test-only'
        client.token_until = float('inf')
        response = SimpleNamespace(ok=False, status_code=429, headers={'Retry-After': '120'})
        client.session.request = MagicMock(return_value=response)
        with self.assertRaises(EtaError) as caught:
            client.request('POST', '/api/v1.0/documentsubmissions', data=b'{}')
        self.assertEqual(caught.exception.retry_after, 121)
        self.assertEqual(client.session.request.call_count, 1)
        self.assertEqual(client.session.request.call_args.kwargs['timeout'], (5, 25))

    def test_invoice_log_explains_each_validation_leaf_without_http_or_writes(self):
        move = self.invoice()
        job = move.eta_job_ids
        job.write({'state': 'blocked', 'last_error': 'ETA rejected: ' + json.dumps({
            'error': {'code': 'ValidationError', 'message': 'Validation failed', 'details': [
                {'code': 'CF313', 'target': 'dateTimeIssued', 'message': 'Issue date outside permitted range'},
                {'code': 'CF305', 'target': 'receiver.id', 'message': 'Receiver ID not registered'}]}})})
        before = (move.invoice_date, move.amount_total, move.write_date, job.write_date, job.state)
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Log performed HTTP')):
            self.assertIn('CF313 / dateTimeIssued', job.reason_summary)
            self.assertIn('CF305 / receiver.id', job.reason_summary)
            self.assertNotIn('Validation failed', job.reason_summary)
            self.assertIn('Keep the original invoice date', job.required_action)
            self.assertIn('customer ID', job.required_action)
            self.assertTrue(job.needs_attention)
        self.assertEqual(before, (move.invoice_date, move.amount_total, move.write_date, job.write_date, job.state))

    def test_invoice_log_recovers_leaf_errors_from_truncated_json(self):
        raw = 'ETA rejected: {"error":{"code":"ValidationError","details":[' + json.dumps({
            'code': 'SF307', 'target': 'receiver.id', 'message': 'Receiver ID length invalid'}) + ',{"code":"CF313"'
        detail, codes = error_details(raw)
        self.assertIn('SF307 / receiver.id: Receiver ID length invalid', detail)
        self.assertEqual(codes, {'ValidationError', 'SF307', 'CF313'})
        job = self.invoice().eta_job_ids
        job.write({'state': 'invalid', 'last_error': raw})
        self.assertIn('customer ID', job.required_action)
        self.assertNotIn('tax calculation', job.required_action)
        self.assertIn('Keep the original invoice date', job.required_action)

    def test_invoice_log_attention_filters_match_live_states_and_attempt_age(self):
        jobs = self.env['tradeline.eta.job']
        queued, accepted, unknown, blocked, approved, reviewed = [self.invoice().eta_job_ids for _ in range(6)]
        now = fields.Datetime.now()
        old = now - timedelta(hours=2)
        all_jobs = queued | accepted | unknown | blocked | approved | reviewed
        self.env.cr.execute('UPDATE tradeline_eta_job SET create_date=%s WHERE id IN %s', [old, tuple(all_jobs.ids)])
        all_jobs.invalidate_recordset(['create_date'])
        all_jobs.modified(['create_date'])
        accepted.write({'state': 'accepted', 'attempted_at': now})
        unknown.state = 'unknown'
        blocked.state = 'blocked'
        approved.state = 'valid'
        reviewed.write({'state': 'valid', 'last_error': 'ETA issue date differs from the original invoice date.'})
        expected = queued | unknown | blocked | reviewed
        base = [('id', 'in', all_jobs.ids)]
        self.assertEqual(set(jobs.search(base + [('needs_attention', '=', True)]).ids), set(expected.ids))
        self.assertEqual(set(jobs.search(base + [('needs_attention', '!=', False)]).ids), set(expected.ids))
        self.assertEqual(set(jobs.search(base + [('needs_attention', '=', False)]).ids), {accepted.id, approved.id})
        self.assertEqual(set(all_jobs.filtered('needs_attention').ids), set(expected.ids))
        self.assertLess(accepted.waiting_minutes, 2)
        accepted.attempted_at = now - timedelta(minutes=20)
        self.assertTrue(accepted.needs_attention)
        self.assertIn(accepted, jobs.search(base + [('needs_attention', '=', True)]))
        self.assertIn('Do not automatically cancel or resend', reviewed.required_action)

    def test_invoice_log_distinguishes_disabled_overdue_and_active_worker(self):
        job = self.invoice().eta_job_ids
        cron = self.env.ref('tradeline_eta_reliability.cron_eta_queue')
        self.env['ir.config_parameter'].sudo().set_param('tradeline_eta_reliability.enabled_database', self.env.cr.dbname)
        cron.write({'active': False, 'failure_count': 0})
        job._compute_monitoring()
        self.assertIn('disabled', job.reason_summary)
        job.last_error = 'ETA temporarily unavailable'
        self.assertIn('Restore the ETA scheduled worker', job.required_action)
        cron.write({'active': True, 'nextcall': fields.Datetime.now() - timedelta(minutes=10)})
        job.last_error = False
        job._compute_monitoring()
        self.assertIn('overdue', job.reason_summary)
        cron.nextcall = fields.Datetime.now() + timedelta(minutes=1)
        job._compute_monitoring()
        self.assertIn('Waiting in the automatic submission queue', job.reason_summary)

    def test_invoice_log_keeps_uncertain_outcomes_and_product_fixes_distinct(self):
        job = self.invoice().eta_job_ids
        job.write({'state': 'unknown', 'last_error': 'Read timed out after submission'})
        self.assertIn('Read timed out', job.reason_summary)
        self.assertIn('before any resend', job.required_action)
        job.write({'state': 'invalid', 'last_error': 'CV302: itemCode is not active'})
        self.assertIn('ETA-approved EGS/GS1', job.required_action)
        job.write({'state': 'invalid', 'last_error': 'DS301: duplicate document'})
        self.assertIn('referenced original ETA receipt', job.required_action)

    def test_invoice_log_navigation_and_incident_drilldown(self):
        job = self.invoice().eta_job_ids
        self.assertEqual(job.action_open_invoice()['res_id'], job.move_id.id)
        monitor = self.env['tradeline.operations.monitor'].with_context(active_test=False).search([], limit=1)
        monitor = monitor or self.env['tradeline.operations.monitor'].create({})
        self.assertEqual(monitor.action_eta_invoice_log()['res_model'], 'tradeline.eta.job')
        for code, state in [('eta:daily_rejected', 'blocked'), ('eta:daily_stalled', 'unknown')]:
            incident = self.env['tradeline.operations.incident'].search([('monitor_id', '=', monitor.id), ('code', '=', code)])
            incident = incident or self.env['tradeline.operations.incident'].create({
                'monitor_id': monitor.id, 'code': code, 'title': 'ETA test incident',
                'severity': 'warning', 'details': 'Test', 'action': 'Review'})
            job.state = state
            self.assertTrue(incident.is_eta_incident)
            action = incident.action_eta_invoice_log()
            self.assertEqual(action['context'], {})
            self.assertIn(job, self.env['tradeline.eta.job'].search(action['domain']))

    def test_invoice_log_accounting_access_respects_selected_company(self):
        job = self.invoice().eta_job_ids
        partner_values = {'name': 'ETA log other company', 'is_company': True, 'vat': '999999999'}
        # The full hosted stack requires size/device data on company contacts.
        # These are rollback-only fixtures; no taxpayer document is submitted.
        for name, value in [('company_size', 'small'), ('company_device', 1)]:
            if name in self.env['res.partner']._fields:
                partner_values[name] = value
        company_partner = self.env['res.partner'].with_context(no_vat_validation=True).create(partner_values)
        company_b = self.env['res.company'].create({'name': 'ETA log other company',
            'partner_id': company_partner.id,
            'activity_code_id': self.company.activity_code_id.id,
            'type_code_id': self.company.type_code_id.id,
            'sub_type_code_id': self.company.sub_type_code_id.id})
        journal_b = self.env['account.journal'].with_company(company_b).create({
            'name': 'ETA log other sales', 'code': 'ELB', 'type': 'sale', 'company_id': company_b.id})
        move_b = self.env['account.move'].with_company(company_b).create({
            'move_type': 'out_invoice', 'journal_id': journal_b.id, 'company_id': company_b.id})
        other_job = self.env['tradeline.eta.job'].create({'move_id': move_b.id, 'state': 'blocked'})
        user = new_test_user(self.env, login='eta_log_manager', groups='account.group_account_manager',
            company_id=self.company.id, company_ids=[Command.set((self.company | company_b).ids)])
        visible = self.env['tradeline.eta.job'].with_user(user).with_context(allowed_company_ids=self.company.ids)
        self.assertEqual(visible.search([('id', 'in', (job | other_job).ids)]), job.with_user(user))
        self.assertTrue(visible.browse(job.id).reason_summary)
        with self.assertRaises(AccessError):
            visible.browse(other_job.id).action_open_invoice()
        with self.assertRaises(AccessError):
            self.env['tradeline.operations.monitor'].with_user(user).action_eta_invoice_log()

    def test_current_eta_date_approval_preserves_invoice_and_archives_previous_attempt(self):
        old = fields.Date.today() - timedelta(days=45)
        move = self.invoice(invoice_date=old)
        job = move.eta_job_ids
        previous = json.dumps({'documents': [{'dateTimeIssued': str(old) + 'T00:00:00Z'}]})
        job.write({'state': 'blocked', 'last_error': 'CF313: outside ETA window', 'payload': previous,
                   'payload_hash': 'previous-hash', 'attempted_at': fields.Datetime.now(), 'safe_to_retry': True})
        before = (move.invoice_date, move.amount_total, move.amount_tax, move.amount_untaxed, move.state)
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Approval performed HTTP')):
            job.action_approve_current_issue_date()
        self.assertEqual(job.state, 'queued')
        self.assertEqual(job.issue_date_policy, 'current')
        self.assertTrue(job.issue_date_approved_by and job.issue_date_approved_at and job.issue_date_approval_note)
        self.assertFalse(job.payload or job.attempted_at or job.current_issue_datetime)
        archive = self.env['ir.attachment'].search([('res_model', '=', 'account.move'), ('res_id', '=', move.id),
            ('name', '=', 'ETA_issue_date_approval_%s.json' % job.id)], limit=1)
        self.assertEqual(json.loads(archive.raw)['old_payload'], previous)
        self.assertEqual(archive.company_id, move.company_id)
        self.assertEqual(before, (move.invoice_date, move.amount_total, move.amount_tax, move.amount_untaxed, move.state))

    def test_current_eta_date_never_approves_known_or_uncertain_outcomes(self):
        for state in ('unknown', 'accepted', 'valid', 'invalid'):
            job = self.invoice().eta_job_ids
            job.state = state
            with self.assertRaises(UserError):
                job.action_approve_current_issue_date()
            self.assertEqual(job.issue_date_policy, 'invoice')
        for values in ({'e_invoice_uuid': 'known'}, {'e_invoice_sent': True}, {'l10n_eg_uuid': 'native-known'}):
            move = self.invoice()
            move.write(values)
            move.eta_job_ids.state = 'blocked'
            with self.assertRaises(UserError):
                move.eta_job_ids.action_approve_current_issue_date()

    def test_current_eta_date_cannot_clear_an_ambiguous_attempt(self):
        job = self.invoice().eta_job_ids
        job.write({'state': 'blocked', 'attempted_at': fields.Datetime.now(), 'safe_to_retry': False})
        with self.assertRaises(UserError):
            job.action_approve_current_issue_date()
        self.assertEqual(job.issue_date_policy, 'invoice')

    def test_current_eta_date_requires_accounting_approval_and_recorded_evidence(self):
        job = self.invoice().eta_job_ids
        user = new_test_user(self.env, login='eta_date_unprivileged', groups='base.group_user',
            company_id=self.company.id, company_ids=[Command.set(self.company.ids)])
        with self.assertRaises(AccessError):
            job.with_user(user).action_approve_current_issue_date()
        job.issue_date_policy = 'current'
        with self.assertRaises(UserError):
            job._prepare()
        self.assertFalse(job.payload or job.attempted_at)

    def test_approved_current_eta_date_is_frozen_for_json_and_canonical(self):
        issued = fields.Date.today() - timedelta(days=45)
        move = self.invoice(invoice_date=issued)
        job = move.eta_job_ids
        job.action_approve_current_issue_date()
        job.current_issue_datetime = fields.Datetime.now()
        expected = job.current_issue_datetime.strftime('%Y-%m-%dT%H:%M:%SZ')
        with patch.object(type(move), '_get_eta_invoice_lines', return_value=([], 0, 100, 100)), \
                patch.object(type(move), '_get_eta_personal_details', side_effect=lambda *a: {'address': {}}), \
                patch.object(type(move), '_get_eta_tax_totals', return_value=[]), \
                patch.object(type(move), 'inv_type', '', create=True), \
                patch.object(type(move), 'reference_number', '', create=True), \
                patch('requests.sessions.Session.request', side_effect=AssertionError('Generation performed HTTP')):
            document, _environment = move.action_generate_eta_json()
        self.assertEqual(document['dateTimeIssued'], expected)
        self.assertIn('"DATETIMEISSUED""%s"' % expected, move.e_invoice_canonical)
        self.assertEqual(move.invoice_date, issued)

    def test_approved_current_eta_date_submits_old_invoice_without_financial_changes(self):
        move = self.invoice(invoice_date=fields.Date.today() - timedelta(days=45))
        before = (move.invoice_date, move.amount_total, move.amount_tax, move.amount_untaxed, move.state)
        job = move.eta_job_ids
        job.action_approve_current_issue_date()
        client = MagicMock()
        client.request.return_value = {'acceptedDocuments': [{'uuid': 'approved-current-date', 'internalId': move.name}]}
        def signed():
            return {'internalID': move.name, 'dateTimeIssued': move._eta_issue_timestamp(),
                    'signatures': [{'type': 'I', 'value': 'test-signature'}]}
        with patch.object(type(move), 'action_generate_eta_json'), \
                patch.object(type(move), 'action_sign_invoice', side_effect=signed), patch.object(self.env.cr, 'commit'):
            job._submit(client)
        self.assertEqual(job.state, 'accepted')
        self.assertEqual(job.attempts, 1)
        self.assertEqual(json.loads(job.payload)['documents'][0]['dateTimeIssued'], move._eta_issue_timestamp())
        self.assertEqual(before, (move.invoice_date, move.amount_total, move.amount_tax, move.amount_untaxed, move.state))
        self.assertEqual(client.request.call_args.args[0], 'POST')

    def test_approved_current_eta_date_reprepares_after_calendar_rollover(self):
        move = self.invoice(invoice_date=fields.Date.today() - timedelta(days=45))
        job = move.eta_job_ids
        job.action_approve_current_issue_date()
        old_time = fields.Datetime.now() - timedelta(days=2)
        job.write({'current_issue_datetime': old_time, 'attempted_at': old_time,
            'last_error': 'ETA returned HTTP 429.', 'payload': json.dumps({'documents': [{
                'internalID': move.name, 'dateTimeIssued': old_time.strftime('%Y-%m-%dT%H:%M:%SZ')}]})})
        client = MagicMock()
        client.request.return_value = {'acceptedDocuments': [{'uuid': 'rollover-approved', 'internalId': move.name}]}
        def signed():
            return {'internalID': move.name, 'dateTimeIssued': move._eta_issue_timestamp(),
                    'signatures': [{'type': 'I', 'value': 'test-signature'}]}
        with patch.object(type(move), 'action_generate_eta_json') as generate, \
                patch.object(type(move), 'action_sign_invoice', side_effect=signed), patch.object(self.env.cr, 'commit'):
            job._submit(client)
        generate.assert_called_once()
        self.assertEqual(job.state, 'accepted')
        self.assertGreater(job.current_issue_datetime, old_time + timedelta(days=1))
        self.assertEqual(move.invoice_date, fields.Date.today() - timedelta(days=45))

    def test_current_eta_date_log_keeps_other_errors_visible(self):
        job = self.invoice().eta_job_ids
        job.action_approve_current_issue_date()
        job.write({'state': 'blocked', 'last_error': 'CF313 / dateTimeIssued; CF305 / receiver ID required'})
        self.assertIn('issue-date treatment is approved', job.required_action)
        self.assertIn('customer ID', job.required_action)
        self.assertNotIn('Finance must arrange', job.required_action)

    def test_current_eta_date_refuses_an_ambiguous_local_invoice_number(self):
        job = self.invoice().eta_job_ids
        with patch.object(type(self.env['account.move']), 'search_count', return_value=2):
            with self.assertRaisesRegex(UserError, 'not unique'):
                job.action_approve_current_issue_date()
        self.assertEqual(job.issue_date_policy, 'invoice')
        self.assertFalse(job.issue_date_approved_at)

    def test_verified_signature_only_rejection_can_be_resigned_without_fiscal_changes(self):
        move = self.invoice()
        move.write({'e_invoice_uuid': 'signature-old', 'e_invoice_status': 'Invalid', 'e_invoice_sent': True})
        original = {'internalID': move.name, 'totalAmount': 100, 'dateTimeIssued': '2026-01-01T00:00:00Z'}
        result = {'uuid': 'signature-old', 'internalId': move.name, 'status': 'Invalid', 'document': original,
                  'validationResults': {'errorCode': 'Err03', 'innerError': [{'errorCode': '4043'}]}}
        client = MagicMock(); client.request.return_value = result
        with patch('odoo.addons.tradeline_eta_reliability.models.account_move.EtaClient', return_value=client), \
                patch.object(type(move), '_eta_correction_document', return_value=original):
            accountant = self.invoice_user()
            accountant.groups_id |= self.env.ref('tradeline_eta_reliability.group_eta_workspace')
            move.with_user(accountant).action_send_electronic_invoice()
        self.assertFalse(move.e_invoice_uuid)
        self.assertEqual(move.eta_job_ids.state, 'queued')
        self.assertEqual(move.amount_total, 100)
        self.assertTrue(self.env['ir.attachment'].search_count([
            ('res_model', '=', 'account.move'), ('res_id', '=', move.id), ('name', '=', 'ETA_rejected_signature-old.json')]))
        self.assertEqual([c.args[0] for c in client.request.call_args_list], ['GET'])

    def test_signature_rejection_with_another_error_stays_held_if_source_unchanged(self):
        move = self.invoice()
        move.write({'e_invoice_uuid': 'signature-old', 'e_invoice_status': 'Invalid', 'e_invoice_sent': True})
        original = {'internalID': move.name, 'totalAmount': 100}
        client = MagicMock()
        client.request.return_value = {'uuid': 'signature-old', 'internalId': move.name, 'status': 'Invalid',
            'document': original, 'validationResults': {'steps': [
                {'errorCode': 'Err03', 'innerError': [{'errorCode': '4043'}]}, {'errorCode': 'NI310'}]}}
        with patch('odoo.addons.tradeline_eta_reliability.models.account_move.EtaClient', return_value=client), \
                patch.object(type(move), '_eta_correction_document', return_value=original):
            with self.assertRaises(UserError):
                move.action_send_electronic_invoice()
        self.assertEqual(move.e_invoice_uuid, 'signature-old')
        self.assertEqual([c.args[0] for c in client.request.call_args_list], ['GET'])

    def test_invoice_number_4043_is_not_a_signature_error_code(self):
        message = 'ETA rejected the submission (1). ' + json.dumps({
            'code': '1', 'target': 'BEVER/2026/4043', 'details': [{
                'code': 'CF313', 'target': 'dateTimeIssued', 'message': 'Issue date outside the submission window'}]})
        reason, codes = error_details(message)
        self.assertIn('CF313', codes)
        self.assertNotIn('4043', codes)
        self.assertIn('Issue date', reason)
        _, signature_codes = error_details("{'errorCode': '4043', 'error': 'Digest mismatch'}")
        self.assertIn('4043', signature_codes)

    def test_untaxed_discounted_eta_lines_match_posted_accounting_amounts(self):
        egp = self.env.ref('base.EGP'); egp.active = True
        metadata = {'e_invoicing_code': 'EG-test-discount', 'code_type': 'egs', 'ar_description': 'Test description'}
        product = self.env['product.product'].create({'name': 'ETA discount test product',
            **{key: value for key, value in metadata.items() if key in self.env['product.product']._fields}})
        eta_uom = self.env['eta.uom'].search([('code', '=', 'D41')], limit=1)
        if not eta_uom:
            eta_uom = self.env['eta.uom'].create({'name': 'Each', 'code': 'D41'})
        product.uom_id.eta_uom_id = eta_uom
        move = self.env['account.move'].create({'move_type': 'out_invoice', 'journal_id': self.journal.id,
            'partner_id': self.partner.id, 'currency_id': egp.id, 'invoice_date': fields.Date.today(),
            'invoice_line_ids': [Command.create({'product_id': product.id, 'account_id': self.income.id,
                'quantity': 2, 'price_unit': 1250, 'discount': 10, 'tax_ids': [Command.clear()]}),
                Command.create({'product_id': product.id, 'account_id': self.income.id,
                'quantity': 1, 'price_unit': 100, 'tax_ids': [Command.clear()]})]})
        move.action_post()
        before = (move.invoice_date, move.amount_total, move.amount_untaxed,
                  move.amount_tax, tuple(move.line_ids.mapped('balance')))
        with ExitStack() as stack:
            stack.enter_context(patch('requests.sessions.Session.request', side_effect=AssertionError('Serialization performed HTTP')))
            # The minimal QA stack lacks optional product master extensions;
            # supply their metadata while exercising the real line serializer.
            for key, value in metadata.items():
                if key not in product._fields:
                    stack.enter_context(patch.object(type(product), key, value, create=True))
            lines, discount, sales, total = move._get_eta_invoice_lines()
        self.assertEqual(lines[0]['salesTotal'], 2500)
        self.assertEqual(lines[0]['netTotal'], 2250)
        self.assertEqual(lines[0]['total'], 2250)
        self.assertEqual(discount, 250)
        self.assertEqual(sales, 2600)
        self.assertEqual(total, 2350)
        self.assertEqual(total, move.amount_total)
        self.assertEqual(before, (move.invoice_date, move.amount_total, move.amount_untaxed,
                  move.amount_tax, tuple(move.line_ids.mapped('balance'))))

    def _description_serializer_fixture(self, **overrides):
        metadata = {'e_invoicing_code': 'EG-test-description', 'code_type': 'egs', 'ar_description': False}
        metadata.update(overrides)
        Product = self.env['product.product']
        product = Product.create({'name': 'Adapter USB-C 25W - White'})
        product.barcode = 'ETA-ENGLISH-DESCRIPTION-%s' % product.id
        product.write({key: value for key, value in metadata.items() if key in product._fields})
        eta_uom = self.env['eta.uom'].search([('code', '=', 'D41')], limit=1)
        eta_uom = eta_uom or self.env['eta.uom'].create({'name': 'Each', 'code': 'D41'})
        product.uom_id.eta_uom_id = eta_uom
        move = self.env['account.move'].create({'move_type': 'out_invoice', 'journal_id': self.journal.id,
            'partner_id': self.partner.id, 'currency_id': self.env.ref('base.EGP').id,
            'invoice_date': fields.Date.today(), 'invoice_line_ids': [Command.create({
                'product_id': product.id, 'account_id': self.income.id, 'quantity': 2,
                'price_unit': 1250, 'discount': 10, 'tax_ids': [Command.clear()]})]})
        move.action_post()
        return move, product, metadata

    def _description_serializer_values(self, move, product, metadata):
        before = (move.invoice_date, move.date, move.amount_total, move.amount_untaxed, move.amount_tax,
                  tuple(move.line_ids.mapped('balance')), move.partner_id.vat,
                  move.e_invoice_uuid, move.e_invoice_status, move.eta_job_ids.payload, move.eta_job_ids.state)
        with ExitStack() as stack:
            stack.enter_context(patch('requests.sessions.Session.request', side_effect=AssertionError('Serialization performed HTTP')))
            for key, value in metadata.items():
                if key not in product._fields:
                    stack.enter_context(patch.object(type(product), key, value, create=True))
            product_before = (product.name, product.barcode,
                product.e_invoicing_code, product.code_type, product.ar_description)
            values = move._get_eta_invoice_lines()
            self.assertEqual(product_before, (product.name, product.barcode,
                product.e_invoicing_code, product.code_type, product.ar_description))
        self.assertEqual(before, (move.invoice_date, move.date, move.amount_total, move.amount_untaxed, move.amount_tax,
                  tuple(move.line_ids.mapped('balance')), move.partner_id.vat,
                  move.e_invoice_uuid, move.e_invoice_status, move.eta_job_ids.payload, move.eta_job_ids.state))
        return values

    def test_actual_eta_description_does_not_require_unused_arabic_metadata(self):
        move, product, metadata = self._description_serializer_fixture()
        lines, discount, sales, total = self._description_serializer_values(move, product, metadata)
        self.assertEqual(lines[0]['description'], 'Adapter USB-C 25W - White')
        self.assertEqual(lines[0]['itemCode'], metadata['e_invoicing_code'])
        self.assertEqual(lines[0]['itemType'], 'egs')
        self.assertEqual(lines[0]['unitType'], 'D41')
        self.assertEqual((discount, sales, total), (250, 2500, move.amount_total))

    def test_reviewed_variant_code_reaches_eta_without_regeneration(self):
        move, product, metadata = self._description_serializer_fixture()
        reviewed_code = 'EG-254598064-REVIEWED-VARIANT'
        if 'e_invoicing_code' in product._fields:
            product.write({'e_invoicing_code': reviewed_code})
        metadata['e_invoicing_code'] = reviewed_code
        lines, _, _, _ = self._description_serializer_values(move, product, metadata)
        self.assertEqual(lines[0]['itemCode'], reviewed_code)
        self.assertEqual(lines[0]['itemType'], metadata['code_type'])
        if 'e_invoicing_code' in product._fields:
            self.assertEqual(product.e_invoicing_code, reviewed_code)

    def test_arabic_metadata_does_not_change_existing_approved_payload(self):
        move, product, metadata = self._description_serializer_fixture(ar_description='Existing catalogue description')
        original = self._description_serializer_values(move, product, metadata)
        payload = json.dumps({'documents': [{'internalID': move.name, 'invoiceLines': original[0]}]})
        move.write({'e_invoice_uuid': 'existing-approved-description', 'e_invoice_status': 'Valid', 'e_invoice_sent': True})
        move.eta_job_ids.write({'state': 'valid', 'payload': payload})
        if 'ar_description' in product._fields:
            product.ar_description = False
        metadata['ar_description'] = False
        self.assertEqual(self._description_serializer_values(move, product, metadata), original)
        self.assertEqual(move.eta_job_ids.payload, payload)

    def test_missing_eta_product_code_or_type_still_blocks_serialization(self):
        for key in ('e_invoicing_code', 'code_type'):
            with self.subTest(required_field=key):
                move, product, metadata = self._description_serializer_fixture(**{key: False})
                with self.assertRaisesRegex(ValidationError, key):
                    self._description_serializer_values(move, product, metadata)
                self.assertFalse(move.eta_job_ids.attempted_at)

    def test_invalid_eta_product_unit_still_blocks_serialization(self):
        move, product, metadata = self._description_serializer_fixture()
        invalid = self.env['eta.uom'].create({'name': 'Invalid ETA unit', 'code': 'INVALID_ETA_UNIT'})
        product.uom_id.eta_uom_id = invalid
        with self.assertRaisesRegex(ValidationError, 'INVALID_ETA_UNIT'):
            self._description_serializer_values(move, product, metadata)
        self.assertFalse(move.eta_job_ids.attempted_at)

    def test_blank_actual_eta_description_is_held_without_fabricating_a_name(self):
        move, product, metadata = self._description_serializer_fixture()
        for description in ('', False, '  \n  '):
            with self.subTest(description=description), patch.object(type(product), 'display_name', description):
                with self.assertRaisesRegex(ValidationError, 'line description is required'):
                    self._description_serializer_values(move, product, metadata)
        self.assertEqual(product.name, 'Adapter USB-C 25W - White')
        self.assertFalse(move.eta_job_ids.attempted_at)

    def _free_item_serializer_fixture(self, currency):
        currency.active = True
        metadata = {'e_invoicing_code': 'EG-test-free', 'code_type': 'egs', 'ar_description': 'Test free item'}
        product = self.env['product.product'].create({'name': 'ETA free item test',
            **{key: value for key, value in metadata.items() if key in self.env['product.product']._fields}})
        eta_uom = self.env['eta.uom'].search([('code', '=', 'D41')], limit=1)
        eta_uom = eta_uom or self.env['eta.uom'].create({'name': 'Each', 'code': 'D41'})
        product.uom_id.eta_uom_id = eta_uom
        vat = self.env['account.tax'].create({'name': 'ETA free item VAT', 'amount': 14,
            'amount_type': 'percent', 'type_tax_use': 'sale', 'price_include_override': 'tax_included'})
        specs = [(1, 114, 0, vat), (2, 0, 0, vat), (3, 0, 0, self.env['account.tax']), (1, 114, 100, vat)]
        move = self.env['account.move'].create({'move_type': 'out_invoice', 'journal_id': self.journal.id,
            'partner_id': self.partner.id, 'currency_id': currency.id, 'invoice_date': fields.Date.today(),
            'invoice_line_ids': [Command.create({'product_id': product.id, 'account_id': self.income.id,
                'quantity': qty, 'price_unit': price, 'discount': disc, 'tax_ids': [Command.set(tax.ids)]})
                for qty, price, disc, tax in specs]})
        move.action_post()
        before = (move.invoice_date, move.amount_total, move.amount_untaxed, move.amount_tax,
                  tuple(move.line_ids.mapped('balance')))
        with ExitStack() as stack:
            stack.enter_context(patch('requests.sessions.Session.request', side_effect=AssertionError('Serialization performed HTTP')))
            for key, value in metadata.items():
                if key not in product._fields:
                    stack.enter_context(patch.object(type(product), key, value, create=True))
            values = move._get_eta_invoice_lines()
        self.assertEqual(before, (move.invoice_date, move.amount_total, move.amount_untaxed,
                                  move.amount_tax, tuple(move.line_ids.mapped('balance'))))
        return move, values

    def test_free_egp_items_have_zero_unit_values_and_balanced_line_totals(self):
        move, (lines, discount, sales, total) = self._free_item_serializer_fixture(self.env.ref('base.EGP'))
        for index in (1, 2):
            self.assertEqual(lines[index]['unitValue']['amountEGP'], 0)
            self.assertEqual(lines[index]['salesTotal'], lines[index]['quantity'] * lines[index]['unitValue']['amountEGP'])
            self.assertEqual(lines[index]['netTotal'], 0)
            self.assertEqual(lines[index]['total'], 0)
        # A paid-price item with a full discount is distinct from a zero-price item.
        self.assertEqual(lines[3]['unitValue']['amountEGP'], 100)
        self.assertEqual(lines[3]['salesTotal'], 100)
        self.assertEqual(lines[3]['discount']['amount'], 100)
        self.assertAlmostEqual(discount, 100, places=4)
        self.assertAlmostEqual(sales, 200, places=4)
        self.assertEqual(total, move.amount_total)

    def test_free_foreign_currency_items_do_not_invent_amount_sold(self):
        _move, (lines, _discount, _sales, _total) = self._free_item_serializer_fixture(self.env.ref('base.USD'))
        for index in (1, 2):
            self.assertEqual(lines[index]['unitValue']['amountSold'], 0)
            self.assertEqual(lines[index]['unitValue']['amountEGP'], 0)
            self.assertEqual(lines[index]['salesTotal'], 0)
            self.assertEqual(lines[index]['total'], 0)
        self.assertEqual(lines[3]['unitValue']['amountSold'], 100)

    def test_eta_canonical_preserves_json_escapes_and_numeric_representation(self):
        move = self.invoice()
        doc = {'description': 'Tablet 10.2"', 'receiver': {'name': 'A\\B\nC'},
               'lines': [{'quantity': 1.0, 'netTotal': 0.0}], 'internalID': 'ABC'}
        expected = (r'"DESCRIPTION""Tablet 10.2\"""RECEIVER""NAME""A\\B\nC"'
                    '"LINES""LINES""QUANTITY""1.0""NETTOTAL""0.0""INTERNALID""ABC"')
        self.assertEqual(move.generate_canonical_manual(doc), expected)

    def test_quote_description_preparation_keeps_fiscal_text_and_signs_escaped_bytes(self):
        move = self.invoice(); job = move.eta_job_ids
        before = (move.invoice_date, move.amount_total, move.amount_tax, move.state)
        description = 'Tablet 10.2"'
        with patch.object(type(move), '_get_eta_invoice_lines', return_value=([{'description': description, 'quantity': 1}], 0, 100, 100)), \
                patch.object(type(move), '_get_eta_personal_details', side_effect=lambda *a: {'address': {}}), \
                patch.object(type(move), '_get_eta_tax_totals', return_value=[]), \
                patch.object(type(move), 'inv_type', '', create=True), \
                patch.object(type(move), 'reference_number', '', create=True), \
                patch.object(type(move), 'get_signature_value', return_value='test-signature'), \
                patch('requests.sessions.Session.request', side_effect=AssertionError('Preparation performed HTTP')):
            job._prepare()
        self.assertEqual(json.loads(job.payload)['documents'][0]['invoiceLines'][0]['description'], description)
        self.assertIn(r'"DESCRIPTION""Tablet 10.2\""', move.e_invoice_canonical)
        self.assertEqual(before, (move.invoice_date, move.amount_total, move.amount_tax, move.state))

    def test_cms_message_digest_must_match_the_document_before_transport(self):
        move = self.invoice(); move.e_invoice_canonical = '"DOCUMENT""test"'
        def signature(digest):
            value = cms.ContentInfo({'content_type': 'signed_data', 'content': {
                'version': 'v3', 'digest_algorithms': [{'algorithm': 'sha256'}],
                'encap_content_info': {'content_type': 'data'}, 'signer_infos': [{
                    'version': 'v3', 'sid': {'subject_key_identifier': b'test-key'},
                    'digest_algorithm': {'algorithm': 'sha256'},
                    'signed_attrs': [{'type': 'message_digest', 'values': [digest]}],
                    'signature_algorithm': {'algorithm': 'rsassa_pkcs1v15'}, 'signature': b'test-only'}]}})
            return base64.b64encode(value.dump()).decode()
        matching = signature(hashlib.sha256(move.e_invoice_canonical.encode()).digest())
        self.assertTrue(move._eta_validate_signature_digest(matching))
        for rejected in (signature(b'0' * 32), 'not base64'):
            with self.assertRaisesRegex(UserError, 'does not match'):
                move._eta_validate_signature_digest(rejected)

    def test_invalid_signing_response_is_stopped_before_eta_post_or_attempt_marker(self):
        move = self.invoice(); job = move.eta_job_ids; client = MagicMock()
        def generate():
            move.write({'e_invoice_json': str({'internalID': move.name}),
                        'e_invoice_canonical': '"INTERNALID""%s"' % move.name})
        with patch.object(type(move), 'action_generate_eta_json', side_effect=generate), \
                patch('odoo.addons.tradeline_eta_reliability.models.account_move.requests.get',
                      return_value=SimpleNamespace(status_code=200, text='not-base64')):
            with self.assertRaisesRegex(UserError, 'Nothing was sent'):
                job._submit(client)
        client.request.assert_not_called()
        self.assertFalse(job.attempted_at or job.payload)
        self.assertEqual(job.attempts, 0)

    def test_negative_exchange_line_is_blocked_before_any_http(self):
        move = self.invoice(); job = move.eta_job_ids
        document = {'invoiceLines': [{'quantity': -1}], 'signatures': [{'value': 'signed-fixture'}]}
        before = (move.invoice_date, move.amount_total, move.amount_tax, move.partner_id.vat)
        with patch.object(type(move), 'action_generate_eta_json'), \
                patch.object(type(move), 'action_sign_invoice', return_value=document), \
                patch('requests.sessions.Session.request', side_effect=AssertionError('Invalid quantity sent HTTP')):
            with self.assertRaisesRegex(UserError, 'positive quantities'):
                job._prepare()
        self.assertFalse(job.payload or job.attempted_at)
        self.assertEqual(before, (move.invoice_date, move.amount_total, move.amount_tax, move.partner_id.vat))

    def test_quantity_contract_keeps_free_goods_and_rejects_nonpositive_or_non_numeric_values(self):
        job = self.invoice().eta_job_ids
        self.assertTrue(job._validate_eta_line_quantities({'invoiceLines': [
            {'quantity': 1, 'unitValue': {'amountEGP': 0}, 'total': 0}, {'quantity': .125}]}))
        for quantity in (-1, 0, float('nan'), float('inf'), '1', True):
            with self.assertRaisesRegex(UserError, 'positive quantities'):
                job._validate_eta_line_quantities({'invoiceLines': [{'quantity': quantity}]})

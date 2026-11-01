import json
from unittest.mock import patch

from odoo import Command, fields
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user
from odoo.tools import SQL


@tagged('post_install', '-at_install')
class TestEtaVatWorkspace(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.ref('base.EGP').active = True
        partner_values = {'name': 'VAT lanes QA company', 'is_company': True,
            'vat': '123456789', 'country_id': cls.env.ref('base.eg').id}
        for name, value in [('company_size', 'small'), ('company_device', 1)]:
            if name in cls.env['res.partner']._fields:
                partner_values[name] = value
        partner = cls.env['res.partner'].with_context(no_vat_validation=True).create(partner_values)
        cls.company = cls.env['res.company'].create({'name': 'VAT lanes QA company', 'partner_id': partner.id,
            'currency_id': cls.env.ref('base.EGP').id, 'country_id': cls.env.ref('base.eg').id,
            'activity_code_id': cls.env.company.activity_code_id.id,
            'type_code_id': cls.env.company.type_code_id.id, 'sub_type_code_id': cls.env.company.sub_type_code_id.id})
        cls.env = cls.env(context=dict(cls.env.context, allowed_company_ids=cls.company.ids))
        cls.company.write({'client_id': 'vat-lanes-qa', 'client_secret': 'qa-only-secret', 'config_type': 'uat'})
        cls.receivable = cls.env['account.account'].create({'name': 'VAT lanes receivable', 'code': 'VAWAR',
            'account_type': 'asset_receivable', 'reconcile': True, 'company_ids': [Command.set(cls.company.ids)]})
        cls.income = cls.env['account.account'].create({'name': 'VAT lanes income', 'code': 'VAWINC',
            'account_type': 'income', 'company_ids': [Command.set(cls.company.ids)]})
        cls.payable_vat = cls.env['account.account'].create({'name': 'VAT lanes tax', 'code': 'VAWVAT',
            'account_type': 'liability_current', 'company_ids': [Command.set(cls.company.ids)]})
        cls.partner = cls.env['res.partner'].create({'name': 'VAT lanes QA buyer',
            'property_account_receivable_id': cls.receivable.id})
        cls.journal = cls.env['account.journal'].create({'name': 'VAT lanes sales', 'code': 'VAW',
            'type': 'sale', 'company_id': cls.company.id, 'edi_format_ids': [Command.clear()]})
        group = cls.env['account.tax.group'].create({'name': 'VAT lanes taxes',
            'company_id': cls.company.id, 'country_id': cls.env.ref('base.eg').id})
        vat_type = cls.env['account.tax.type.code'].search([('code','=','T1')], limit=1)
        vat_type = vat_type or cls.env['account.tax.type.code'].create({'code':'T1','ar_name':'VAT'})
        cls.vat = cls.env['account.tax'].create({'name': 'VAT lanes 14%', 'amount': 14,
            'type_tax_use': 'sale', 'type_code_id': vat_type.id, 'tax_group_id': group.id,
            'price_include_override': 'tax_included'})
        (cls.vat.invoice_repartition_line_ids | cls.vat.refund_repartition_line_ids).filtered(
            lambda row: row.repartition_type == 'tax').account_id = cls.payable_vat
        cls.clerk = new_test_user(cls.env, login='eta_vat_lanes_clerk', groups='account.group_account_invoice',
            company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)])

    def setUp(self):
        super().setUp()
        guard = patch('requests.sessions.Session.request', side_effect=AssertionError('VAT lanes attempted real HTTP'))
        guard.start(); self.addCleanup(guard.stop)

    def invoice(self, posted=True, vat=False, kind='out_invoice', original=None):
        move = self.env['account.move'].with_context(eta_queue_cron=True).create({
            'move_type': kind, 'journal_id': self.journal.id, 'partner_id': self.partner.id,
            'invoice_date': fields.Date.today(), 'eta_vat_original_id': original.id if original else False,
            'invoice_line_ids': [Command.create({'name': 'VAT lane item', 'account_id': self.income.id,
                'price_unit': 100, 'quantity': 1, 'tax_ids': [Command.set(self.vat.ids if vat else [])]})]})
        if posted:
            move.action_post()
        return move

    def receipt(self, move, status='Valid', missing=True):
        uuid = 'vat-lane-%s' % move.id
        move.write({'e_invoice_uuid': uuid, 'e_invoice_status': status, 'e_invoice_sent': True})
        move.eta_job_ids.sudo().write({'state': 'valid' if status == 'Valid' else 'invalid'})
        self.assertTrue(move._eta_record_receipt_vat(uuid, {'internalID': move.name,
            'issuer': {'id': self.company.vat}, 'taxTotals': [{'taxType':'T1','amount':0 if missing else 14}]}))

    def assert_route(self, move, route):
        self.assertEqual(move.eta_vat_workflow, route)
        model = self.env['account.move']
        self.assertIn(move, model.search([('id','=',move.id),('eta_vat_workflow','=',route)]))
        self.assertIn(move, model.search([('id','=',move.id),('eta_vat_workflow','in',[route])]))
        self.assertNotIn(move, model.search([('id','=',move.id),('eta_vat_workflow','!=',route)]))
        self.assertNotIn(move, model.search([('id','=',move.id),('eta_vat_workflow','not in',[route])]))

    def test_never_sent_draft_and_posted_invoices_use_original_not_credit_route(self):
        for move in (self.invoice(posted=False), self.invoice()):
            self.assertTrue(move._eta_can_reset_unsent())
            self.assert_route(move, 'unsent')
            self.assertIn('No credit note is needed', move.eta_vat_workflow_note)
        self.assert_route(self.invoice(vat=True), False)
        self.assert_route(self.invoice(kind='out_refund'), False)

    def test_approved_zero_vat_requires_matching_current_persisted_receipt_facts(self):
        move = self.invoice(); self.receipt(move)
        self.assert_route(move, 'approved')
        self.assertIn('Odoo accounting only', move.eta_vat_workflow_note)
        move.sudo().eta_receipt_vat_uuid = 'earlier-uuid'
        self.assert_route(move, 'review')

    def test_native_pending_delivery_is_review_even_with_a_valid_receipt(self):
        move = self.invoice(); self.receipt(move)
        self.assert_route(move, 'approved')
        edi_format = self.env['account.edi.format'].search([('code','=','eg_eta')], limit=1)
        self.assertTrue(edi_format)
        document = self.env['account.edi.document'].create({
            'move_id': move.id, 'edi_format_id': edi_format.id, 'state': 'to_send'})
        self.assert_route(move, 'review')
        document.state = 'to_cancel'
        self.assert_route(move, 'review')
        document.state = 'sent'
        self.assert_route(move, 'approved')

    def test_unstamped_and_noncustomer_rows_skip_local_amendment_tax_probes(self):
        healthy = self.invoice(vat=True)
        credit = self.invoice(kind='out_refund')
        self.env['account.move']._eta_flush_vat_workflow()
        for move, expected_aggregate_loops in ((healthy, 1), (credit, 0)):
            self.env.cr.execute(SQL(
                'EXPLAIN (ANALYZE, FORMAT JSON) SELECT %s FROM account_move WHERE id=%s',
                move._eta_vat_workflow_sql(), move.id))
            plan = self.env.cr.fetchone()[0][0]['Plan']
            nodes = [plan]
            aggregate_loops = 0
            while nodes:
                node = nodes.pop()
                nodes.extend(node.get('Plans', []))
                if node['Node Type'] == 'Aggregate':
                    aggregate_loops += node['Actual Loops']
            self.assertEqual(aggregate_loops, expected_aggregate_loops,
                'Only the current-invoice VAT probe may run; local amendment probes require audit stamps.')

    def test_positive_route_scope_keeps_inverse_and_empty_route_membership(self):
        approved = self.invoice(); self.receipt(approved)
        healthy = self.invoice(vat=True)
        credit = self.invoice(kind='out_refund')
        model = self.env['account.move']
        scope = [('id','in',(approved | healthy | credit).ids)]
        for operator, value in (('!=','approved'), ('not in',['approved'])):
            self.assertEqual(model.search(scope + [('eta_vat_workflow',operator,value)]), healthy | credit)
        self.assertEqual(model.search(scope + [('eta_vat_workflow','in',['approved',False])]),
                         approved | healthy | credit)
        self.assertFalse(model.search(scope + [('eta_vat_workflow','not in',['approved',False])]))

    def test_unknown_historical_or_conflicting_receipt_is_never_an_unsent_invoice(self):
        for change in ({'state':'unknown','attempts':1,'attempted_at':fields.Datetime.now()},
                       {'historical_review':True}, {'submission_uuid':'unconfirmed-submission'}):
            move = self.invoice(); move.eta_job_ids.sudo().write(change)
            self.assert_route(move, 'review')
        move = self.invoice(); self.receipt(move)
        move.l10n_eg_uuid = 'conflicting-native-receipt'
        self.assert_route(move, 'review')

    def test_invalid_can_use_existing_invoice_but_receiver_rejected_requires_review(self):
        invalid = self.invoice(); self.receipt(invalid, status='Invalid')
        self.assert_route(invalid, 'rejected')
        self.assertIn('No credit note is needed', invalid.eta_vat_workflow_note)
        rejected = self.invoice(); self.receipt(rejected, status='Rejected')
        self.assert_route(rejected, 'review')
        invalid.eta_job_ids.state = 'unknown'
        self.assert_route(invalid, 'review')

    def test_archived_legacy_receipt_prevents_never_sent_classification(self):
        move = self.invoice()
        self.env['ir.attachment'].create({'name':'ETA_legacy.json','res_model':'account.move',
            'res_id':move.id,'company_id':self.company.id,'mimetype':'application/json',
            'raw':json.dumps({'request':{'internalID':move.name},'response':{'l10n_eg_uuid':'old'}}).encode()})
        self.assertFalse(move._eta_can_reset_unsent())
        self.assert_route(move, 'review')

    def test_existing_corrections_are_opened_and_approved_credit_is_not_recreated(self):
        original = self.invoice(); self.receipt(original)
        credit = self.invoice(posted=False, kind='out_refund', original=original)
        replacement = self.invoice(posted=False, vat=True, original=original)
        self.assert_route(original, 'in_progress')
        self.assertIn('existing credit note first', original.eta_vat_workflow_note)
        credit.action_post(); self.receipt(credit)
        self.assert_route(original, 'in_progress')
        self.assertIn('already approved by ETA', original.eta_vat_workflow_note)
        self.assertIn('Do not create or send another credit', original.eta_vat_workflow_note)
        replacement.button_cancel()
        self.assert_route(original, 'in_progress')
        self.assertIn('no active replacement', original.eta_vat_workflow_note)

    def test_completed_requires_both_approved_documents_and_positive_replacement_receipt(self):
        original = self.invoice(); self.receipt(original)
        credit = self.invoice(kind='out_refund', original=original); self.receipt(credit)
        replacement = self.invoice(vat=True, original=original); self.receipt(replacement, missing=False)
        self.assertEqual(original.eta_vat_review_state, 'corrected')
        self.assert_route(original, 'completed')
        replacement.sudo().eta_receipt_vat_missing = True
        self.assert_route(original, 'in_progress')

    def test_search_preserves_invoice_record_rules_and_returns_no_wide_id_list(self):
        allowed, hidden = self.invoice(), self.invoice()
        hidden_healthy = self.invoice(vat=True)
        for move in allowed | hidden:
            self.receipt(move)
        rule = self.env['ir.rule'].create({'name':'VAT lanes inaccessible branch invoice',
            'model_id':self.env['ir.model']._get_id('account.move'),
            'domain_force':"[('id','not in',%s)]" % (hidden | hidden_healthy).ids})
        try:
            found = self.env['account.move'].with_user(self.clerk).search([
                ('id','in',(allowed | hidden).ids),('eta_vat_workflow','=','approved')])
            self.assertEqual(found, allowed.with_user(self.clerk))
            domain = self.env['account.move'].with_user(self.clerk)._search_eta_vat_workflow('=', 'approved')
            self.assertFalse(isinstance(domain[0][2], (list,tuple)), 'Use a scoped Query, not enumerated historical invoice IDs.')
            mixed = self.env['account.move'].with_user(self.clerk)._search_eta_vat_workflow('in', ['approved', False])
            scoped_ids = mixed[0][2].get_result_ids()
            self.assertNotIn(hidden.id, scoped_ids)
            self.assertNotIn(hidden_healthy.id, scoped_ids, 'An OR with the empty route must not escape the inner record-rule scope.')
        finally:
            rule.unlink()

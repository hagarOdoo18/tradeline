"""Receipt-absence presentation cannot become a submission/replay permission."""
import hashlib
import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user


@tagged('post_install', '-at_install')
class TestEtaDeliveryReview(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        values = {'vat': '123456789'}
        for name, value in [('company_size', 'small'), ('company_device', 1)]:
            if name in cls.company.partner_id._fields:
                values[name] = value
        cls.company.partner_id.with_context(no_vat_validation=True).write(values)
        cls.company.write({'client_id': 'delivery-review-test', 'client_secret': 'qa-only-secret',
                           'config_type': 'uat', 'config_version': '1.0', 'signature_tool': 'python'})
        cls.env.ref('base.EGP').active = True
        cls.receivable = cls.env['account.account'].create({'name': 'Delivery QA receivable', 'code': 'DLVAR',
            'account_type': 'asset_receivable', 'reconcile': True, 'company_ids': [Command.set(cls.company.ids)]})
        cls.income = cls.env['account.account'].create({'name': 'Delivery QA income', 'code': 'DLVINC',
            'account_type': 'income', 'company_ids': [Command.set(cls.company.ids)]})
        cls.partner = cls.env['res.partner'].create({'name': 'Delivery QA buyer',
            'property_account_receivable_id': cls.receivable.id})
        cls.journal = cls.env['account.journal'].create({'name': 'Delivery QA sales', 'code': 'DLV',
            'type': 'sale', 'company_id': cls.company.id, 'edi_format_ids': [Command.clear()]})
        cls.clerk = new_test_user(cls.env, login='eta_delivery_review_clerk', groups='account.group_account_invoice',
            company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)])
        cls.admin = new_test_user(cls.env, login='eta_delivery_review_admin', groups='base.group_system,account.group_account_manager',
            company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)])

    def setUp(self):
        super().setUp()
        guard = patch('requests.sessions.Session.request', side_effect=AssertionError('Delivery review made real HTTP'))
        guard.start(); self.addCleanup(guard.stop)
        self.Job = self.env['tradeline.eta.job']

    def unknown_job(self):
        move = self.env['account.move'].with_context(eta_queue_cron=True).create({
            'move_type': 'out_invoice', 'journal_id': self.journal.id, 'partner_id': self.partner.id,
            'currency_id': self.env.ref('base.EGP').id, 'invoice_date': fields.Date.today(),
            'invoice_line_ids': [Command.create({'name': 'Delivery QA item', 'account_id': self.income.id,
                'quantity': 1, 'price_unit': 100, 'tax_ids': [Command.clear()]})]})
        move.action_post()
        payload = json.dumps({'documents': [{'internalID': move.name, 'issuer': {'id': self.company.vat},
            'documentType': 'I', 'totalAmount': 100, 'signatures': [{'value': 'retained-qa-signature'}]}]})
        job = move.eta_job_ids.sudo()
        job.write({'state': 'unknown', 'attempts': 1, 'attempted_at': fields.Datetime.now()-timedelta(minutes=20),
            'payload': payload, 'payload_hash': hashlib.sha256(payload.encode()).hexdigest()})
        return job

    def proof(self, job):
        end = fields.Datetime.now()-timedelta(seconds=2)
        return {'version': 1, 'source': 'eta_documents_search', 'database': self.env.cr.dbname,
            'job': job.id, 'invoice': job.move_id.id, 'company_id': job.company_id.id,
            'internalId': job.move_id.name, 'attempts': job.attempts,
            'attempted_at': fields.Datetime.to_string(job.attempted_at), 'payload_sha256': job.payload_hash,
            'pagination_complete': True, 'receipt_count': 0, 'receipts': [],
            'search_start_utc': fields.Datetime.to_string(job.attempted_at-timedelta(days=1)),
            'search_end_utc': fields.Datetime.to_string(end),
            'checked_at_utc': fields.Datetime.to_string(fields.Datetime.now())}

    def test_complete_absence_changes_staff_group_only_and_preserves_finance_and_transport(self):
        job = self.unknown_job(); move = job.move_id
        before = job.read(['state','payload','payload_hash','attempted_at','attempts','safe_to_retry','next_attempt'])[0]
        financial = move.read(['amount_total','amount_tax','invoice_date','date','state','line_ids'])[0]
        self.assertTrue(job._record_delivery_receipt_absence(self.proof(job)))
        self.assertEqual(job.read(list(before.keys()))[0], before)
        self.assertEqual(move.read(list(financial.keys()))[0], financial)
        self.assertEqual(job.staff_state, 'delivery_review')
        self.assertEqual(move.with_user(self.clerk).eta_staff_state, 'delivery_review')
        result = job.with_user(self.clerk).read(['staff_state','staff_summary','required_action'])[0]
        self.assertIn('No receipt was found', result['staff_summary'])
        self.assertIn('remains unconfirmed', result['staff_summary'])
        self.assertIn('ETA delivery confirmation', result['required_action'])
        groups = self.Job.with_user(self.clerk).read_group([('id','=',job.id)], ['staff_state'], ['staff_state'])
        self.assertEqual(groups[0]['staff_state'], 'delivery_review')
        audit = self.env['ir.attachment'].search([('res_model','=','tradeline.eta.job'),('res_id','=',job.id),('name','=like','ETA_delivery_review_%')])
        self.assertEqual(len(audit), 1); self.assertFalse(audit.public)

    def test_marker_or_identity_changes_immediately_invalidate_the_classification(self):
        job = self.unknown_job(); job._record_delivery_receipt_absence(self.proof(job))
        original = job.read(['state','payload','payload_hash','attempted_at','attempts'])[0]
        for change in ({'attempts': 2}, {'payload_hash': 'different'}, {'payload': job.payload+' '},
                       {'attempted_at': job.attempted_at+timedelta(seconds=1)}):
            with self.subTest(change=change):
                job.write(change)
                self.assertEqual(job.staff_state, 'unknown')
                job.write({key: original[key] for key in change})
                self.assertEqual(job.staff_state, 'delivery_review')
        job.state = 'valid'
        self.assertEqual(job.staff_state, 'valid')

    def test_incomplete_wrong_request_or_invalid_window_proof_cannot_classify(self):
        job = self.unknown_job(); proof = self.proof(job)
        cases = [{'pagination_complete': False}, {'receipt_count': 1}, {'receipts': [{'uuid':'another'}]},
            {'database': 'another-database'}, {'job': job.id+1}, {'invoice': job.move_id.id+1},
            {'company_id': job.company_id.id+1}, {'internalId': 'another-invoice'}, {'attempts': 2},
            {'payload_sha256': 'wrong'}, {'attempted_at': fields.Datetime.to_string(job.attempted_at+timedelta(seconds=1))},
            {'search_start_utc': fields.Datetime.to_string(job.attempted_at+timedelta(seconds=1))},
            {'search_end_utc': fields.Datetime.to_string(job.attempted_at-timedelta(seconds=1))},
            {'checked_at_utc': fields.Datetime.to_string(fields.Datetime.now()+timedelta(minutes=2))}]
        for change in cases:
            with self.subTest(change=change):
                with self.assertRaises(UserError):
                    job._record_delivery_receipt_absence(dict(proof, **change))
                self.assertFalse(job.delivery_review_proof)
                self.assertEqual(job.staff_state, 'unknown')

    def test_named_admin_rpc_cannot_forge_or_copy_private_review_facts(self):
        job = self.unknown_job(); actor = job.with_user(self.admin)
        self.assertFalse(actor.env.su)
        for values in ({'delivery_review_proof': json.dumps(self.proof(job))}, {'staff_state': 'delivery_review'}):
            with self.assertRaises(AccessError): actor.write(values)
            with self.assertRaises(AccessError):
                self.Job.with_user(self.admin).create(dict(values, move_id=job.move_id.id))
        with self.assertRaises(AccessError): actor._record_delivery_receipt_absence(self.proof(job))

    def test_classification_does_not_authorize_post_and_later_exact_receipt_can_resolve(self):
        job = self.unknown_job(); job._record_delivery_receipt_absence(self.proof(job))
        proof = job.delivery_review_proof
        self.assertTrue(job._staff_prepare_send())
        self.assertEqual(job.state, 'unknown')
        self.assertEqual(job.attempts, 1)
        client = MagicMock()
        document = json.loads(job.payload)['documents'][0]
        def response(method, path, **kwargs):
            self.assertEqual(method, 'GET', 'A delivery review must never send a second invoice')
            if path.endswith('/search'):
                return {'result':[{'uuid':'late-exact','internalId':job.move_id.name}],
                    'metadata':{'continuationToken':'EndofResultSet'}}
            return {'uuid':'late-exact','internalId':job.move_id.name,'status':'Valid','document':document}
        client.request.side_effect = response
        job._staff_check_status(client)
        self.assertEqual(job.state, 'valid')
        self.assertEqual(job.staff_state, 'valid')
        self.assertEqual(job.delivery_review_proof, proof, 'Keep the earlier historical review audit')
        self.assertEqual(job.attempts, 1)


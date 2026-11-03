"""Run in `odoo-bin shell` against a disposable LOCAL QA database only.

Uses real commits/cursors/advisory locks and synthetic invoices. Every HTTP call
is forbidden; only a fake ETA transport and signing/preflight fixtures are used.
Synthetic accounting/audit records remain in the disposable QA database.
"""
import hashlib
import json
from contextlib import ExitStack
from datetime import datetime
from unittest.mock import patch

import requests
from odoo import Command, fields
from odoo.addons.tradeline_eta_reliability.models import staff_workflow as staff

assert env.cr.dbname == 'eta_reliability_qa_20261028', 'Disposable local QA database required'
assert env['ir.config_parameter'].sudo().get_param('web.base.url', '').startswith(('http://localhost', 'http://127.0.0.1')), 'Local QA URL required'
company = env.company
original = company.read(['client_id', 'client_secret', 'config_type', 'eta_authority_retry_at'])[0]
original_vat = company.partner_id.vat
egp = env.ref('base.EGP')
original_currency_active = egp.active
params = env['ir.config_parameter'].sudo()
binding = params.get_param('tradeline_eta_reliability.enabled_database')
stamp = datetime.utcnow().strftime('%H%M%S')
posts = []
lost_response = [False]
documents = {}


class FakeEta:
    def __init__(self, company):
        self.company = company
        self.staff_error = False

    def _authorize(self):
        pass

    def close(self):
        pass

    def request(self, method, path, **kwargs):
        if method == 'POST':
            document = json.loads(kwargs['data'])['documents'][0]
            with env.registry.cursor() as check:
                check.execute('SELECT j.state,j.attempts,j.payload_hash FROM tradeline_eta_job j '
                    'JOIN account_move m ON m.id=j.move_id WHERE m.name=%s AND m.journal_id=%s',
                    [document['internalID'], journal.id])
                state, attempts, digest = check.fetchone()
            assert (state, attempts) == ('unknown', 1), 'Durable marker must be visible BEFORE POST'
            assert digest == hashlib.sha256(kwargs['data']).hexdigest()
            posts.append(document['internalID'])
            if lost_response[0]:
                raise requests.ReadTimeout('Synthetic lost response; never real HTTP')
            documents['qa-receipt-' + stamp] = document
            return {'acceptedDocuments': [{'internalId': document['internalID'], 'uuid': 'qa-receipt-' + stamp}],
                    'submissionUUID': 'qa-submission-' + stamp}
        assert method == 'GET', 'No cancellation or unexpected operation permitted'
        if path.endswith('/search'):
            return {'result': [], 'metadata': {'continuationToken': 'EndofResultSet'}}
        document = documents['qa-receipt-' + stamp]
        receipt = {'uuid': 'qa-receipt-' + stamp, 'internalId': document['internalID'], 'status': 'Valid', 'document': document}
        return {'documentSummary': [receipt]} if '/documentsubmissions/' in path else receipt


def prepare(job):
    document = {'internalID': job.move_id.name, 'issuer': {'id': company.vat},
        'dateTimeIssued': job.move_id._eta_issue_timestamp(), 'invoiceLines': [{'quantity': 1}],
        'signatures': [{'value': 'QA-ONLY-NO-REAL-SIGNATURE'}], 'totalAmount': 100}
    payload = json.dumps({'documents': [document]}, ensure_ascii=False, separators=(',', ':'))
    job.write({'payload': payload, 'payload_hash': hashlib.sha256(payload.encode()).hexdigest()})


def invoice():
    move = env['account.move'].with_context(eta_queue_cron=True).create({
        'move_type': 'out_invoice', 'journal_id': journal.id, 'partner_id': partner.id,
        'currency_id': env.ref('base.EGP').id, 'invoice_date': fields.Date.today(),
        'invoice_line_ids': [Command.create({'name': 'Synthetic direct-send QA only',
            'account_id': income.id, 'quantity': 1, 'price_unit': 100, 'tax_ids': [Command.clear()]})]})
    move.action_post()
    env.cr.commit()
    return move.with_user(clerk)


try:
    with patch('requests.sessions.Session.request', side_effect=AssertionError('Real HTTP is forbidden')):
        company.write({'client_id': 'QA-only', 'client_secret': 'QA-only', 'config_type': 'uat', 'eta_authority_retry_at': False})
        egp.active = True
        company.partner_id.with_context(no_vat_validation=True).vat = '123456789'
        params.set_param('tradeline_eta_reliability.enabled_database', env.cr.dbname)
        receivable = env['account.account'].create({'name': 'Synthetic QA receivable', 'code': 'TQAR' + stamp,
            'account_type': 'asset_receivable', 'reconcile': True, 'company_ids': [Command.set(company.ids)]})
        income = env['account.account'].create({'name': 'Synthetic QA income', 'code': 'TQINC' + stamp,
            'account_type': 'income', 'company_ids': [Command.set(company.ids)]})
        partner = env['res.partner'].create({'name': 'Synthetic transaction QA buyer',
            'property_account_receivable_id': receivable.id})
        journal = env['account.journal'].create({'name': 'Synthetic ETA QA ' + stamp, 'code': 'T' + stamp[-4:],
            'type': 'sale', 'company_id': company.id, 'edi_format_ids': [Command.clear()]})
        clerk = env['res.users'].with_context(no_reset_password=True).create({'name': 'Synthetic QA invoice clerk',
            'login': 'eta-transaction-qa-' + stamp, 'company_id': company.id,
            'company_ids': [Command.set(company.ids)], 'groups_id': [Command.set([
                env.ref('base.group_user').id, env.ref('account.group_account_invoice').id])]})
        env.cr.commit()
        with ExitStack() as stack:
            stack.enter_context(patch.object(staff, 'EtaClient', FakeEta))
            stack.enter_context(patch.object(staff, 'StatusClient', FakeEta))
            stack.enter_context(patch.object(type(env['tradeline.eta.job']), '_prepare', autospec=True, side_effect=prepare))
            stack.enter_context(patch.object(type(env['tradeline.eta.job']), '_preflight_submission', return_value=True))
            move = invoice()
            request = move.action_eta_send_now()['params']['request_id']; env.cr.commit()
            with env.registry.cursor() as backlog:
                backlog.execute('SELECT pg_advisory_lock(741181,1)')
                try:
                    result = move.action_eta_execute_staff_request(request)
                finally:
                    backlog.execute('SELECT pg_advisory_unlock(741181,1)')
            assert 'Received by ETA' in result['title'] and len(posts) == 1
            replay = move.action_eta_execute_staff_request(request)
            assert 'already started' in replay['title'] and len(posts) == 1
            env.cr.rollback(); env.invalidate_all()
            status_request = move.action_eta_check_status_now()['params']['request_id']; env.cr.commit()
            checked = move.action_eta_execute_staff_request(status_request)
            assert checked['title'] == 'Approved by ETA' and len(posts) == 1
            uncertain = invoice(); lost_response[0] = True
            request = uncertain.action_eta_send_now()['params']['request_id']; env.cr.commit()
            unknown = uncertain.action_eta_execute_staff_request(request)
            assert unknown['title'] == 'ETA delivery verification' and len(posts) == 2
            env.cr.rollback(); env.invalidate_all()
            prior = uncertain.eta_job_ids.sudo().read(['payload_hash', 'attempted_at', 'attempts'])[0]
            request = uncertain.action_eta_send_now()['params']['request_id']; env.cr.commit()
            unknown = uncertain.action_eta_execute_staff_request(request)
            env.cr.rollback(); env.invalidate_all()
            after = uncertain.eta_job_ids.sudo().read(['payload_hash', 'attempted_at', 'attempts'])[0]
            assert unknown['title'] == 'ETA delivery verification' and len(posts) == 2 and prior == after
            print('STAFF_REAL_TRANSACTION_PROOF', json.dumps({'non_admin': True, 'busy_backlog_bypassed': True,
                'durable_marker_visible_before_post': True, 'replay_did_not_post': True,
                'status_get_only': True, 'lost_response_not_resent': True, 'synthetic_posts': len(posts)}))
finally:
    env.cr.rollback(); env.invalidate_all()
    company.write({key: original[key] for key in ('client_id', 'client_secret', 'config_type', 'eta_authority_retry_at')})
    company.partner_id.with_context(no_vat_validation=True).vat = original_vat
    egp.active = original_currency_active
    params.set_param('tradeline_eta_reliability.enabled_database', binding)
    env.cr.commit()

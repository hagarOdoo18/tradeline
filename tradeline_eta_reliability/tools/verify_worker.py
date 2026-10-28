"""Run in an isolated Odoo shell; real transactions, fully mocked ETA transport."""
import json
from datetime import datetime
from unittest.mock import MagicMock

import requests
from odoo import api, Command, fields

assert env.cr.dbname == 'eta_reliability_qa_20261028', 'Refusing to create fixtures outside the isolated QA database'
env.company.write({'client_id': 'qa-only', 'client_secret': 'qa-only', 'config_type': 'uat'})
suffix = datetime.now().strftime('%H%M%S')
Account = env['account.account']
receivable = Account.create({'name': 'Worker QA receivable', 'code': 'WQARCV' + suffix,
    'account_type': 'asset_receivable', 'reconcile': True, 'company_ids': [Command.set(env.company.ids)]})
income = Account.create({'name': 'Worker QA income', 'code': 'WQAINC' + suffix,
    'account_type': 'income', 'company_ids': [Command.set(env.company.ids)]})
partner = env['res.partner'].create({'name': 'Worker QA', 'property_account_receivable_id': receivable.id})
journal = env['account.journal'].create({'name': 'Worker QA', 'code': 'W' + suffix[-3:], 'type': 'sale',
    'edi_format_ids': [Command.clear()]})
moves = env['account.move']
for index in range(3):
    moves |= env['account.move'].create({'move_type': 'out_invoice', 'journal_id': journal.id,
        'partner_id': partner.id, 'invoice_date': fields.Date.today(),
        'invoice_line_ids': [Command.create({'name': 'Worker QA', 'account_id': income.id,
            'price_unit': 100, 'tax_ids': [Command.clear()]})]})
moves.action_post()
jobs = moves.eta_job_ids.sorted('id')
for job in jobs:
    job.payload = json.dumps({'documents': [{'internalID': job.move_id.name, 'dateTimeIssued': str(job.move_id.invoice_date) + 'T00:00:00Z'}]})
job_ids, move_ids = jobs.ids, moves.ids
env.cr.commit()

client = MagicMock()
posts = []

def transport(method, path, **kwargs):
    assert method == 'POST', 'Unexpected request in accepted-submission test'
    name = json.loads(kwargs['data'])['documents'][0]['internalID']
    posts.append(name)
    return {'submissionUUID': 'worker-qa-submission', 'acceptedDocuments': [
        {'uuid': 'worker-qa-' + str(len(posts)), 'internalId': name, 'longId': 'worker-qa-long'}]}

client.request.side_effect = transport
with env.registry.cursor() as locked:
    locked.execute('SELECT id FROM account_move WHERE id=%s FOR UPDATE', [move_ids[0]])
    env['tradeline.eta.job']._process_id(job_ids[0], client)
    env['tradeline.eta.job']._process_id(job_ids[1], client)
    assert len(posts) == 1, 'A locked invoice prevented the next job or caused an extra POST'
    locked.rollback()
env.invalidate_all()
assert env['tradeline.eta.job'].browse(job_ids[0]).state == 'queued'
assert env['tradeline.eta.job'].browse(job_ids[1]).state == 'accepted'
env.cr.rollback()

client.request.side_effect = requests.Timeout('Simulated lost POST response')
env['tradeline.eta.job']._process_id(job_ids[2], client)
env.invalidate_all()
assert env['tradeline.eta.job'].browse(job_ids[2]).state == 'unknown', 'The durable attempt marker was lost'
env.cr.rollback()
client.request.reset_mock()
client.request.side_effect = None
client.request.return_value = {'result': [], 'metadata': {'continuationToken': 'EndofResultSet'}}
env['tradeline.eta.job']._process_id(job_ids[2], client)
assert all(c.args[0] == 'GET' for c in client.request.call_args_list), 'An uncertain POST was replayed'
env.cr.rollback()
print('WORKER_TRANSACTION_QA_PASSED: lock isolation, following-job progress, durable uncertain marker, no ambiguous replay')

company_b = env['res.company'].create({'name': 'ETA Worker QA B ' + suffix,
    'currency_id': env.company.currency_id.id, 'activity_code_id': env.company.activity_code_id.id,
    'type_code_id': env.company.type_code_id.id, 'sub_type_code_id': env.company.sub_type_code_id.id,
    'client_id': 'qa-only-company-b', 'client_secret': 'qa-only-company-b', 'config_type': 'uat'})
account_b = Account.with_company(company_b)
receivable_b = account_b.create({'name': 'Worker B receivable', 'code': 'BQARCV' + suffix,
    'account_type': 'asset_receivable', 'reconcile': True, 'company_ids': [Command.set(company_b.ids)]})
income_b = account_b.create({'name': 'Worker B income', 'code': 'BQAINC' + suffix,
    'account_type': 'income', 'company_ids': [Command.set(company_b.ids)]})
partner_b = env['res.partner'].with_company(company_b).create({'name': 'Worker QA B',
    'property_account_receivable_id': receivable_b.id})
journal_b = env['account.journal'].with_company(company_b).create({'name': 'Worker QA B',
    'code': 'B' + suffix[-3:], 'type': 'sale', 'company_id': company_b.id, 'edi_format_ids': [Command.clear()]})
move_b = env['account.move'].with_company(company_b).create({'move_type': 'out_invoice',
    'journal_id': journal_b.id, 'company_id': company_b.id, 'partner_id': partner_b.id,
    'invoice_date': fields.Date.today(), 'invoice_line_ids': [Command.create({'name': 'Worker QA B',
        'account_id': income_b.id, 'price_unit': 100, 'tax_ids': [Command.clear()]})]})
move_b.action_post()
job_b = move_b.eta_job_ids
job_b.payload = json.dumps({'documents': [{'internalID': move_b.name, 'dateTimeIssued': str(move_b.invoice_date) + 'T00:00:00Z'}]})
job_b_id, move_b_id, company_b_id, invoice_b_name = job_b.id, move_b.id, company_b.id, move_b.name
env.cr.commit()
client_b = MagicMock()
client_b.request.side_effect = [
    {'submissionUUID': 'company-b-submission', 'acceptedDocuments': [
        {'uuid': 'company-b-uuid', 'internalId': invoice_b_name, 'longId': 'company-b-long'}]},
    {'documentSummary': [{'uuid': 'company-b-uuid', 'internalId': invoice_b_name, 'status': 'Valid'}]},
    {'uuid': 'company-b-uuid', 'internalId': invoice_b_name, 'status': 'Valid',
     'submissionUUID': 'company-b-submission', 'document': json.dumps({'internalID': invoice_b_name})},
]
env['tradeline.eta.job']._process_id(job_b_id, client_b)
env['tradeline.eta.job']._process_id(job_b_id, client_b)
env.cr.rollback()
env.invalidate_all()
verified_b = env['account.move'].sudo().browse(move_b_id)
assert verified_b.e_invoice_status == 'Valid'
assert verified_b.l10n_eg_eta_json_doc_id.company_id.id == company_b_id, 'ETA receipt assigned to the wrong company'
env.cr.rollback()
print('WORKER_COMPANY_QA_PASSED: second-company submission and receipt ownership with first-company caller context')

"""Run in an isolated Odoo shell; real transactions, fully mocked ETA transport."""
import json
from unittest.mock import MagicMock

import requests
from odoo import api, Command, fields

assert env.cr.dbname == 'eta_reliability_qa_20261028', 'Refusing to create fixtures outside the isolated QA database'
env.company.write({'client_id': 'qa-only', 'client_secret': 'qa-only', 'config_type': 'uat'})
Account = env['account.account']
receivable = Account.create({'name': 'Worker QA receivable', 'code': 'WQARCV',
    'account_type': 'asset_receivable', 'reconcile': True, 'company_ids': [Command.set(env.company.ids)]})
income = Account.create({'name': 'Worker QA income', 'code': 'WQAINC',
    'account_type': 'income', 'company_ids': [Command.set(env.company.ids)]})
partner = env['res.partner'].create({'name': 'Worker QA', 'property_account_receivable_id': receivable.id})
journal = env['account.journal'].create({'name': 'Worker QA', 'code': 'WQA', 'type': 'sale',
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
    job.payload = json.dumps({'documents': [{'internalID': job.move_id.name}]})
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

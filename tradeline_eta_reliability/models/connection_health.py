"""Bounded read-only ETA health probes and measured delivery/approval timings."""
import time
from datetime import timedelta

import requests

from odoo import api, fields, models, _
from odoo.exceptions import AccessError

from .client import EtaClient, EtaError
from .processing_window import check_processing_window, EtaStopped


class EtaHealthCompany(models.Model):
    _inherit = 'res.company'

    eta_health_checked_at = fields.Datetime(readonly=True)
    eta_health_seconds = fields.Float(readonly=True)
    eta_health_state = fields.Selection([('ok','Available'), ('slow','Responding slowly'),
        ('connection','Connection issue'), ('access','ETA access issue'), ('waiting','ETA retry deadline'),
        ('paused','Processing paused')], readonly=True)
    eta_health_environment = fields.Char(readonly=True)

    @api.model
    def _cron_eta_connection_probe(self):
        if self.env['ir.config_parameter'].sudo().get_param(
                'tradeline_eta_reliability.enabled_database') != self.env.cr.dbname:
            return
        companies = self.sudo().search([('client_id','!=',False),('client_secret','!=',False)])
        for company in companies:
            self.env.cr.execute('SELECT pg_try_advisory_xact_lock(741184,%s)', [company.id])
            if not self.env.cr.fetchone()[0]:
                continue
            now = fields.Datetime.now()
            if company.eta_health_checked_at and company.eta_health_checked_at > now-timedelta(seconds=55):
                continue
            state, seconds, client = 'connection', 0, None
            started = time.monotonic()
            try:
                check_processing_window(self.env)
                if company.eta_authority_retry_at and company.eta_authority_retry_at > now:
                    state = 'waiting'
                else:
                    client = EtaClient(company)
                    # Published authenticated lookup endpoint; no tax document,
                    # invoice identifier, submission or cancellation is sent.
                    client.request('GET', '/api/v1.0/documenttypes')
                    seconds = time.monotonic()-started
                    state = 'slow' if seconds >= 5 else 'ok'
            except EtaStopped:
                state = 'paused'
            except EtaError as exc:
                seconds = time.monotonic()-started
                state = 'access' if exc.status in (401,403) else 'waiting' if exc.status == 429 else 'connection'
                if exc.status == 429:
                    company.eta_authority_retry_at = now+timedelta(seconds=exc.retry_after)
            except requests.RequestException:
                seconds = time.monotonic()-started
            finally:
                if client is not None:
                    client.close()
            company.write({'eta_health_checked_at': fields.Datetime.now(), 'eta_health_seconds': seconds,
                'eta_health_state': state, 'eta_health_environment': company.config_type})


class MeasuredSubmissionClient:
    def __init__(self, client):
        self.client, self.seconds = client, None

    def __getattr__(self, name):
        return getattr(self.client, name)

    def request(self, method, path, **kwargs):
        started = time.monotonic()
        result = self.client.request(method, path, **kwargs)
        if method == 'POST' and path == '/api/v1/documentsubmissions':
            self.seconds = time.monotonic()-started
        return result


class EtaMeasuredJob(models.Model):
    _inherit = 'tradeline.eta.job'

    send_response_seconds = fields.Float(readonly=True, digits=(16, 5), groups='account.group_account_invoice,account.group_account_readonly')
    send_response_measured_at = fields.Datetime(readonly=True)
    first_approval_observed_at = fields.Datetime(readonly=True)
    approval_delay_seconds = fields.Float(readonly=True)

    def _submit(self, client):
        measured = MeasuredSubmissionClient(client)
        result = super()._submit(measured)
        if measured.seconds is not None:
            self.write({'send_response_seconds': measured.seconds, 'send_response_measured_at': fields.Datetime.now()})
        return result

    def _apply_remote(self, data):
        already_approved = self.state == 'valid'
        result = super()._apply_remote(data)
        if data.get('status') == 'Valid' and not already_approved and not self.first_approval_observed_at:
            now = fields.Datetime.now()
            self.first_approval_observed_at = now
            if self.attempted_at:
                self.approval_delay_seconds = max(0, (now-self.attempted_at).total_seconds())
        return result

    @api.model
    def get_staff_connection_status(self):
        if not (self.env.su or self.env.user.has_group('account.group_account_invoice')
                or self.env.user.has_group('account.group_account_readonly')):
            raise AccessError(_('Accounting access is required.'))
        enabled = self.env['ir.config_parameter'].sudo().get_param(
            'tradeline_eta_reliability.enabled_database') == self.env.cr.dbname
        now = fields.Datetime.now()
        rows = []
        # Timings use this reader's invoice/branch rules, just like the list.
        for company in self.env.companies:
            company = company.sudo()
            state = company.eta_health_state or 'unchecked'
            checked = company.eta_health_checked_at
            if not enabled:
                state = 'disabled'
            elif not checked or company.eta_health_environment != company.config_type:
                state = 'unchecked'
            elif checked < now-timedelta(minutes=3):
                state = 'stale'
            common = [('company_id','=',company.id)]
            send = self.read_group(common+[('send_response_measured_at','>=',now-timedelta(hours=1))], ['send_response_seconds:avg'], [])
            approval = self.read_group(common+[('first_approval_observed_at','>=',now-timedelta(hours=1)),
                ('approval_delay_seconds','>',0)], ['approval_delay_seconds:avg'], [])
            rows.append({'company_id':company.id, 'company':company.name, 'state':state,
                'checked_at': fields.Datetime.to_string(checked) if checked else False,
                'response_seconds':round(company.eta_health_seconds,2),
                'send_seconds':round(send[0]['send_response_seconds'],2) if send and send[0].get('__count') else False,
                'send_count':send[0].get('__count',0) if send else 0,
                'approval_seconds':round(approval[0]['approval_delay_seconds'],1) if approval and approval[0].get('__count') else False,
                'approval_count':approval[0].get('__count',0) if approval else 0})
        return rows

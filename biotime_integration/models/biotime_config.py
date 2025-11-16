# -*- coding: utf-8 -*-
import logging
from odoo import fields, models, api, _
from odoo.exceptions import ValidationError
import requests, json

_logger = logging.getLogger(__name__)
from datetime import timedelta, datetime, date
import pytz

_tzs = [(tz, tz) for tz in sorted(pytz.all_timezones, key=lambda tz: tz if not tz.startswith('Etc/') else '_')]


def _tz_get(self):
    return _tzs


class BioTime(models.Model):
    _name = 'biotime.config'
    _description = "Bio Config"

    name = fields.Char(string="Name")
    server_url = fields.Char(string="Server URL", default="http://102.222.252.74:8081")
    username = fields.Char(string="Username")
    password = fields.Char(string="Password")
    company_id = fields.Many2one('res.company', string="Company", default=lambda self: self.env.company.id)
    tz = fields.Selection(
        _tz_get, string='Timezone', required=True,
        default=lambda self: self._context.get('tz') or self.env.user.tz or self.env.ref('base.user_admin').tz or 'UTC',
        help="This field is used in order to define in which timezone the resources will work.")

    pull_from_date = fields.Datetime('Pull From Date')
    pull_to_date = fields.Datetime('Pull To Date')

    def action_pull_specific_dates(self):
        for rec in self.env['biotime.config'].sudo().search([]):
            rec.action_get_today_attendance(from_date=rec.pull_from_date, to_date=rec.pull_to_date)

    def generate_access_token(self):
        for rec in self:
            url = "%s/jwt-api-token-auth/" % rec.server_url
            payload = json.dumps({
                "username": rec.username,
                "password": rec.password
            })
            headers = {
                'Content-Type': 'application/json'
            }
            response = requests.request("POST", url, headers=headers, data=payload,verify=False)
            _logger.info(response.text)
            _logger.info(response.status_code)
            print(response.json())
            return response.json()

    def action_get_all_terminals(self):
        for rec in self:
            terminal_env = self.env['biotime.terminal'].sudo()
            url = "%s/iclock/api/terminals/" % rec.server_url

            payload = {}
            headers = {
                'Content-Type': 'application/json',
                'Authorization': 'JWT %s' % rec.generate_access_token().get('token')
            }

            response = requests.request("GET", url, headers=headers, data=payload,verify=False)
            print(response.text)
            data = response.json()
            if data.get('data'):
                for terminal in data['data']:
                    check = terminal_env.search([
                        ('biotime_id', '=', rec.id),
                        ('terminal_sn', '=', terminal.get('sn')),
                    ])
                    if not check:
                        if  int(terminal.get('state') ) == 1:
                            state = 'online'
                        elif int(terminal.get('state') ) == 3:
                            state = 'offline'
                        elif int(terminal.get('state') ) == 2:
                            state = 'communicated'
                        else:
                            state = 'unauthorized'
                        terminal_env.create({
                            'name': terminal.get('terminal_name') if terminal.get('terminal_name') else 'New Device',
                            'terminal_id': terminal.get('id'),
                            'terminal_sn': terminal.get('sn'),
                            'ip_address': terminal.get('ip_address'),
                            'alias': terminal.get('alias'),
                            'terminal_tz': terminal.get('terminal_tz'),
                            'state': state,
                            'biotime_id': rec.id
                        })

    def action_get_all_employees(self, page=1):
        for rec in self:
            employee_env = self.env['biotime.employee'].sudo()
            url = "%s/personnel/api/employees/?page=%s" % (rec.server_url, page)

            payload = {}
            headers = {
                'Content-Type': 'application/json',
                'Authorization': 'JWT %s' % rec.generate_access_token().get('token')
            }

            response = requests.request("GET", url, headers=headers, data=payload,verify=False)
            print(response.text)
            data = response.json()
            if data.get('data'):
                for employee in data['data']:
                    check = employee_env.search([
                        ('biotime_id', '=', rec.id),
                        ('employee_id', '=', employee.get('id')),
                    ])
                    if not check:
                        employee_env.create({
                            'name': employee.get('first_name'),
                            'employee_id': employee.get('id'),
                            'emp_code': employee.get('emp_code'),
                            'biotime_id': rec.id
                        })
                    else:
                        check.emp_code = employee.get('emp_code')
                        check.employee_id = employee.get('id')
            if data.get('next'):
                self.action_get_all_employees(page=data.get('next').split('page=')[1])

    def cron_get_today_attendance(self):
        for rec in self.env['biotime.config'].sudo().search([]):
            rec.action_get_today_attendance()

    def convert_to_utc(self, date, timezone):

        # Define the format of your datetime string
        date_format = '%Y-%m-%d %H:%M:%S'

        # Convert the datetime string to a datetime object
        local_dt = datetime.strptime(date, date_format)

        # Localize the datetime object to your local time zone
        local_tz = pytz.timezone(timezone)
        local_dt = local_tz.localize(local_dt)

        # Convert the localized datetime to UTC
        utc_dt = local_dt.astimezone(pytz.utc)
        str_time = str(utc_dt)
        if '+' in str_time:
            str_time = str_time.split('+')[0]
        return str_time

    def action_get_today_attendance(self, from_date=False, to_date=False):
        biotime_transaction_obj=self.env['biotime.transaction']
        local_tz = pytz.timezone(self.env.user.partner_id.tz or 'GMT')

        for rec in self:
            for tz in self.env['biotime.terminal'].sudo().search([('biotime_id', '=', rec.id)]):
                if from_date and to_date:
                    transactions = tz.action_get_transactions(from_date=rec.pull_from_date,
                                                              to_date=rec.pull_to_date).get('data')
                else:
                    from_date_today = datetime.now() - timedelta(days=7)
                    to_date_today = datetime.now() + timedelta(days=1)
                    transactions = tz.action_get_transactions(from_date=from_date_today,
                                                              to_date=to_date_today).get('data')
                if transactions:
                    records = sorted(transactions, key=lambda x: x['punch_time'])
                    for record in records:
                        str_time = rec.convert_to_utc(record.get('punch_time'), rec.tz)
                        _logger.info('str_time %s - %s' % (str_time, record.get('punch_time')))
                        check_employee = self.env['hr.employee'].sudo().with_company(self.env.company.id).search([
                            ('employee_code', '=', record.get('emp_code')),
                        ], limit=1)
                        # Creating transaction record
                        punch_time = fields.Datetime.to_datetime(
                            record['punch_time'])
                        # Local timezone datetime obj
                        local_dt = local_tz.localize(
                            punch_time, is_dst=None)
                        # utc timezone datetime obj
                        utc_dt = local_dt.astimezone(pytz.utc)
                        # utc_dt = utc_dt.strftime("%Y-%m-%d %H:%M:%S")
                        punch_time = utc_dt.strftime("%Y-%m-%d %H:%M:%S")
                        # punch_time = datetime.strptime(
                        #     utc_dt, "%Y-%m-%d %H:%M:%S")
                        # punch_time = fields.Datetime.to_string(punch_time)
                        punch_time = fields.Datetime.to_datetime(
                            punch_time)
                        if not biotime_transaction_obj.search(
                                [('employee_code', '=', record['emp_code']), ('punch_time', '=', punch_time)]):
                            employee =  self.env['hr.employee'].sudo().with_company(tz.company_id.id).search([
                                    ('employee_code', '=', record.get('emp_code')),('company_id','=',tz.company_id.id)
                                ], limit=1)

                            biotime_transaction_obj.create({
                                "server_id": tz.id,
                                "punch_state": str(record['punch_state']),
                                "verify_type": str(record['verify_type']),
                                "punch_time": punch_time,
                                "employee_id": employee.id if employee else False,
                                "bio_id": record['id'],
                                "terminal_alias": record['terminal_alias'],
                                "terminal_sn": record['terminal_sn'],
                                "employee_code": record['emp_code'],
                            })






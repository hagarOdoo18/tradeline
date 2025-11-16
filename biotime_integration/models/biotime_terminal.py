# -*- coding: utf-8 -*-
import logging
from odoo import fields, models, api, _
from odoo.exceptions import ValidationError
import requests, json

_logger = logging.getLogger(__name__)


class BioTimeTerminal(models.Model):
    _name = 'biotime.terminal'
    _description = 'Bio Time Terminal'

    name = fields.Char(string="Name")
    terminal_id = fields.Char(string="Terminal ID")
    terminal_sn = fields.Char(string="Terminal SN")
    ip_address = fields.Char(string="IP Address")
    alias = fields.Char(string="Alias")
    terminal_tz = fields.Char(string="Terminal TZ")
    biotime_id = fields.Many2one('biotime.config', string="Biotime")
    company_id = fields.Many2one('res.company', string="Company", default=lambda self: self.env.company.id)
    state = fields.Selection(
        string='State',
        selection=[('unauthorized', 'Unauthorized'),
                   ('online', 'Online'), ('communicated','Communicated'),('offline','Offline')],
        required=False, )
        
    def action_get_transactions(self, page=1, from_date=fields.Datetime.now(), to_date=fields.Datetime.now()):
        for rec in self:
            today = from_date.replace(hour=0, minute=0, second=0)
            today_end = to_date.replace(hour=23, minute=59, second=59)
            url = "%s/iclock/api/transactions/?page=%s&page_size=1000000000&limit=1000000&terminal_sn=%s&start_time=%s&end_time=%s" % (
            rec.biotime_id.server_url, page, rec.terminal_sn, today, today_end)
            _logger.info("Today %s - %s" % (today, today_end))
            payload = {}
            headers = {
                'Content-Type': 'application/json',
                'Authorization': 'JWT %s' % rec.biotime_id.generate_access_token().get('token')
            }

            response = requests.request("GET", url, headers=headers, data=payload,verify=False)
            _logger.info("response %s" % (response.json()))
            print(response.json())
            return response.json()

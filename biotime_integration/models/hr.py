# -*- coding: utf-8 -*-
import logging
from odoo import fields, models, api, _
from odoo.exceptions import ValidationError
import requests, json
_logger = logging.getLogger(__name__)


class HrAttendance(models.Model):
    _inherit = 'hr.attendance'

    check_date = fields.Date(string="Date", compute="_compute_check_date", store=True)

    @api.depends('check_in')
    def _compute_check_date(self):
        for rec in self:
            rec.check_date = False
            if rec.check_in:
                rec.check_date = rec.check_in.date()





class Employee(models.Model):
    _inherit = 'hr.employee'

    bio_code = fields.Char(
        string='Biotime code',
        required=False)


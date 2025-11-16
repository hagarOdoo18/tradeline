   # -*- coding: utf-8 -*-
import logging
from odoo import fields, models, api, _
from odoo.exceptions import ValidationError
import requests, json


class BioTimeEmployee(models.Model):
    _name = 'biotime.employee'
    _description = 'Bio Time Employee'

    name = fields.Char(string="Name")
    emp_code = fields.Char(string="Employee Code")
    employee_id = fields.Char(string="Employee ID")
    odoo_employee_id = fields.Many2one('hr.employee', string="Odoo Employee")
    biotime_id = fields.Many2one('biotime.config', string="Biotime")
    company_id = fields.Many2one('res.company', string="Company", default=lambda self: self.env.company.id)

    def get_employee(self):
        for rec in self:
            employee = self.env['hr.employee'].search([('bio_code','=',rec.emp_code)],limit=1)
            if employee:
                rec.write({'odoo_employee_id':employee.id})


    @api.model_create_multi
    def create(self, values):
        # Add code here
        records= super(BioTimeEmployee, self).create(values)
        records.get_employee()
        return records

    # @api.constrains('odoo_employee_id')
    # def check_validate_odoo_employee_id(self):
    #     for rec in self:
    #         search = self.sudo().search([
    #             ('odoo_employee_id','=', rec.odoo_employee_id.id),
    #             ('id','!=', rec.id),
    #         ])
    #         if search:
    #             raise ValidationError(_("You can't set odoo employee in more than one record"))
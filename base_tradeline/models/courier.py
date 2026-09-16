from odoo import fields, models, api


class Courier(models.Model):
    _name = 'courier.courier'
    _description = 'Courier'

    name= fields.Char(
        string='Name',
        required=True)
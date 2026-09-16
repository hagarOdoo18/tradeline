from odoo import fields, models, api


class Channel(models.Model):
    _name = 'channel.channel'
    _description = 'Channel'

    name= fields.Char(
        string='Name',
        required=True)
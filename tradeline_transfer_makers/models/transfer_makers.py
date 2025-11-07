from odoo import fields, models, api
import random
from odoo.fields import datetime
from datetime import datetime


class TransferMakers(models.Model):
    _name = 'transfer.makers'
    _rec_name = 'name'

    code = fields.Char(string='Code',track_visibility='always',)
    name = fields.Char(string='Name',track_visibility='always',)
    transfer_line_ids = fields.One2many(
        comodel_name='transfer.makers.line',
        inverse_name='transfer_maker_id',
        string='Transfer_line_ids',
        required=False)

    mail = fields.Char(
        string='EMail',
        required=False)

    _sql_constraints = [
        ('code', 'unique(code)', 'Unique Code.')
    ]


    @api.model
    def _name_search(self, name='', args=None, operator='ilike', limit=100, name_get_uid=None):
        # private implementation of name_search, allows passing a dedicated user
        # for the name_get part to solve some access rights issues
        args = list(args or [])
        # optimize out the default criterion of ``ilike ''`` that matches everything
        if not (name == '' and operator == 'ilike'):
            args += ['|', ('name', operator, name), ('code', '=', name)]
        access_rights_uid = name_get_uid or self._uid
        ids = self._search(args, limit=limit, access_rights_uid=access_rights_uid)
        recs = self.browse(ids)
        return recs.sudo(access_rights_uid).name_get()


class TransferMakersLine(models.Model):
    _name = 'transfer.makers.line'

    transfer_maker_id = fields.Many2one(
        comodel_name='transfer.makers',
        string='Transfer_maker_id',
        required=False)

    stock_picking = fields.Many2one(
        comodel_name='stock.picking',domain=[('picking_type_code','=','internal')],
        string='Transfer',
        required=True)

    date = fields.Datetime(
       string='Date',default=datetime.today(),readonly=True,
       required=False)

    code = fields.Char(
        string='Code',readonly=True,store=True,
        required=False)
    def generate_code(self):
        if not self.code:
            a = datetime.now()
            self.code =str(int(a.strftime('%Y%m%d')))+str(random.randint(100,9999))
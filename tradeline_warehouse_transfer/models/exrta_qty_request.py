from odoo import fields, models, api


class CancelReason(models.TransientModel):
    _name = 'extra.qty.wizard'

    note = fields.Char()

    request_id = fields.Many2one(
        comodel_name='transfer.request',
        string='Request_id',
        required=False)

    @api.model
    def default_get(self, fields):

        rec = super(CancelReason, self).default_get(fields)
        rec['request_id'] = self.env.context.get('active_ids')[0]
        rec['note'] =  str(self.env.context.get('product')) + " Not enough QTY in the source warehouse Max QTY ["+str(self.env.context.get('qty')) +"]"

        return rec

    def approve_request(self):

        self.request_id.transfer_ids = [(6,0, [self.request_id.create_first_transfer(),self.request_id.create_second_transfer()])]
        self.request_id.state = 'approved'
        self.request_id.transfers_count = 2
        self.request_id._tradeline_refresh_source_documents()

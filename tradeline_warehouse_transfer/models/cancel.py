from odoo import fields, models, api


class CancelReason(models.TransientModel):
    _name = 'cancel.reason.wizard'

    note = fields.Char(string="Reason")

    request_id = fields.Many2one(
        comodel_name='transfer.request',
        string='Request_id',
        required=False)

    @api.model
    def default_get(self, fields):
        rec = super(CancelReason, self).default_get(fields)
        rec['request_id'] = self.env.context.get('active_ids')[0]
        return rec
    def cancel_request(self):
        self.request_id.action_cancel()
        self.request_id.message_post(body= "Cancel Reason [ "+ str(self.note)+" ]")

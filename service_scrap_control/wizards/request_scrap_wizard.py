from odoo import fields, models


class RequestScrapWizard(models.TransientModel):
    _name = 'request.scrap.wizard'
    _description = 'Request Scrap Wizard'

    picking_id = fields.Many2one('stock.picking', required=True)
    note = fields.Text(required=True)

    def action_request_scrap(self):
        self.ensure_one()
        self.picking_id.action_request_scrap(self.note)
        return {'type': 'ir.actions.act_window_close'}

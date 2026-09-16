from odoo import fields, models


class RequestScrapWizard(models.TransientModel):
    # The legacy scrap module owns request.scrap.wizard.  Reusing its name
    # replaces one implementation depending on module loading order.
    _name = 'service.request.scrap.wizard'
    _description = 'Request Scrap Wizard'

    picking_id = fields.Many2one('stock.picking', required=True)
    note = fields.Text(required=True)

    def action_request_scrap(self):
        self.ensure_one()
        self.picking_id.action_request_scrap(self.note)
        return {'type': 'ir.actions.act_window_close'}

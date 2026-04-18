from odoo import fields, models, _


class StockScrap(models.Model):
    _name = 'stock.scrap'
    _inherit = ['stock.scrap', 'mail.thread', 'mail.activity.mixin']

    state = fields.Selection(
        selection_add=[('witting', 'Waiting For Approve'), ('approve', 'Approved')],
        ondelete={'witting': 'set default', 'approve': 'set default'},
    )

    def request_request(self):
        self.ensure_one()
        view = self.env.ref('tradeline_stock_scrap_custom.view_request_scrap_wizard')
        return {
            'name': _('Request Scrap'),
            'view_mode': 'form',
            'res_model': 'request.scrap.wizard',
            'view_id': view.id,
            'views': [(view.id, 'form')],
            'type': 'ir.actions.act_window',
            'context': {'default_scrap_id': self.id},
            'target': 'new',
        }

    def function_approve_scrap(self):
        self.message_post(body=_("Approve Scrap"))
        self.state = 'approve'

from odoo import fields, models, _
from odoo.exceptions import UserError


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    approve_scrap = fields.Boolean(string='Approve')
    wait_approve_scrap = fields.Boolean(string='Waiting For Approve')
    request_scrap = fields.Boolean(string='Request Scrap')
    move_ids_without_package = fields.One2many(
        'stock.move', 'picking_id', string="Stock move",
        domain=['|', ('package_level_id', '=', False), ('picking_type_entire_packs', '=', False),('scrapped', '=', False)],)
    check_scrap_new = fields.Boolean(compute="check_scrap",
                                    string='Check_type',
                                    required=False)


    def check_scrap(self):
        for rec in self:
            if self.env.user.has_group('tradeline_stock_scrap_custom.group_picking_scrap') and rec.picking_type_id.code in [
                'incoming', 'outgoing']:
                rec.check_scrap_new = True
            elif self.env.user.has_group(
                    'tradeline_stock_scrap_custom.group_picking_scrap_transfer') and rec.picking_type_id.code not in [
                'incoming', 'outgoing']:
                rec.check_scrap_new = True
            else:
                rec.check_scrap_new = False

    def button_scrap(self):
        if not self.approve_scrap:
            raise UserError(_("Not Approved Yet!!!"))
        self.ensure_one()
        view = self.env.ref('tradeline_stock_scrap_custom.view_stock_scrap_wizard')
        return {
            'name': _('Scrap'),
            'view_mode': 'form',
            'res_model': 'stock.scrap.wizard',
            'view_id': view.id,
            'views': [(view.id, 'form')],
            'type': 'ir.actions.act_window',
            'context': {'default_picking_id': self.id},
            'target': 'new',
        }

    def button_vendor(self):
        self.ensure_one()
        view = self.env.ref('tradeline_stock_scrap_custom.view_stock_scrap_wizard')
        return {
            'name': _('Vendor Scrap'),
            'view_mode': 'form',
            'res_model': 'stock.scrap.wizard',
            'view_id': view.id,
            'views': [(view.id, 'form')],
            'type': 'ir.actions.act_window',
            'context': {'default_picking_id': self.id, 'default_vendor': True},
            'target': 'new',
        }

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
            'context': {'default_picking_id': self.id},
            'target': 'new',
        }

    def function_approve_scrap(self):
        self.approve_scrap = True
        self.wait_approve_scrap = False
        self.message_post(body=_("Approve Scrap"))

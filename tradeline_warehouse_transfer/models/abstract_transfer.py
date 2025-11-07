from odoo import fields, models, api




class BaseTransfer(models.AbstractModel):
    _name = 'base.transfer'


    def create_enter(self):
        # leave it empty
        pass
    def approve_request(self):
        # leave it empty
        pass

    def is_pos_mrp_order_installed(self):
        target_module = self.env['ir.module.module'].search([('name', '=', 'kasb_managed_warehouse_mrp')])
        if not target_module or target_module.state != 'installed':
            return False
        return True
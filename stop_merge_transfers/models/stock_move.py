# -*- coding: utf-8 -*-
from odoo import models

class StockMove(models.Model):
    _inherit = "stock.move"

    def _search_picking_for_assignation_domain(self):
        if self.picking_type_id.code not in [ 'outgoing','internal']:
            domain = [
                ('group_id', '=', self.group_id.id),
                ('location_id', '=', self.location_id.id),
                ('location_dest_id', '=',
                 (self.location_dest_id.id or self.picking_type_id.default_location_dest_id.id)),
                ('picking_type_id', '=', self.picking_type_id.id),
                ('printed', '=', False),
                ('state', 'in', ['draft'])]
        else:
            domain = [
                ('group_id', '=', self.group_id.id),
                ('location_id', '=', self.location_id.id),
                ('location_dest_id', '=',
                 (self.location_dest_id.id or self.picking_type_id.default_location_dest_id.id)),
                ('picking_type_id', '=', self.picking_type_id.id),
                ('printed', '=', False),
                ('state', 'in', ['draft', 'confirmed', 'waiting', 'partially_available', 'assigned'])]

        if self.partner_id and not self.group_id:
            domain += [('partner_id', '=', self.partner_id.id)]
        return domain

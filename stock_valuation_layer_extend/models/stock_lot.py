# -*- coding: utf-8 -*-
from odoo import _, models
from odoo.exceptions import UserError
from odoo.tools import float_compare


class StockLot(models.Model):
    _inherit = 'stock.lot'

    def write(self, vals):
        if 'standard_price' in vals and not self.env.context.get('disable_auto_svl'):
            if vals['standard_price'] < 0:
                raise UserError(_('A lot or serial number cannot be given a negative cost.'))
            for lot in self:
                product = lot.product_id.with_company(self.env.company)
                if (product.categ_id.property_cost_method == 'average' and
                        product.categ_id.property_valuation == 'real_time' and
                        float_compare(lot.quantity_svl, 0.0, precision_rounding=product.uom_id.rounding) > 0 and
                        float_compare(product.quantity_svl, 0.0, precision_rounding=product.uom_id.rounding) <= 0):
                    raise UserError(_(
                        'The product valuation quantity is zero or negative. Reconcile valuation quantity '
                        'before changing the cost of %(serial)s.',
                        serial=lot.display_name,
                    ))
        return super().write(vals)

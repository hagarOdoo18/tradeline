import logging
from odoo import api, fields, models
_logger = logging.getLogger(__name__)


class StockQuant(models.Model):
    _inherit = 'stock.quant'

    @api.model
    def pos_check_quantity(self, session_id, quantities):
        quantities = {int(k): int(val) for k, val in quantities.items()}
        _logger.info("Demanded Quantities: {}".format(quantities))
        session = self.env['pos.session'].sudo().browse(session_id)
        location = session.config_id.picking_type_id.default_location_src_id
        _logger.info("Session: {}, Location: {}".format(session.name, location.complete_name))
        product_ids = list(quantities.keys())
        products = {product.id: product.name for product in self.env['product.product'].browse(product_ids).filtered(lambda p: p.type == 'product')}
        quants = self.env['stock.quant'].sudo().search(
            ['|', ('location_id', '=', location.id), ('location_id', 'child_of', location.id),
             ('product_id', 'in', product_ids)])
        available = {quant.product_id.id: quant.quantity for quant in quants}
        _logger.info("Available quantities: {}".format(available))
        res = [[product_id, qty, available[product_id] if product_id in available else 0] for product_id, qty in
               quantities.items()]
        return {'location': location.complete_name,
                'lines': [[products[product_id], qty, available] for product_id, qty, available in res if
                          qty > available and product_id in products]}

# models/pos_order.py
from odoo import models, fields, api


class PosOrder(models.Model):
    _inherit = 'pos.order'


    discount_reason_id = fields.Many2one(
        'discount.reason',
        string='Discount Reason',
        help='Reason for applying discount on this order'
    )

    as_gift = fields.Boolean(
        string='As Gift',
        default=False,
        help='Mark this order as a gift'
    )

    sales_rep_id = fields.Many2one(
        'sales.rep',
        string='Sales Representative',
        help='Employee who made this sale'
    )

    @api.model
    def _order_fields(self, ui_order):
        """Add discount_reason, as_gift and sales_rep_id to order fields"""
        order_fields = super(PosOrder, self)._order_fields(ui_order)

        # Add discount reason from UI order
        if ui_order.get('discount_reason_id'):
            order_fields['discount_reason_id'] = ui_order['discount_reason_id']

        # Add as_gift from UI order
        if ui_order.get('as_gift'):
            order_fields['as_gift'] = ui_order['as_gift']

        # Add sales rep from UI order
        if ui_order.get('sales_rep_id'):
            order_fields['sales_rep_id'] = ui_order['sales_rep_id']

        return order_fields

    def _prepare_order_line(self, line):
        """Prepare order line data"""
        line_data = super(PosOrder, self)._prepare_order_line(line)
        return line_data

    def _create_invoice(self, move_vals):
        """Override to auto print invoice after creation"""
        move = super()._create_invoice(move_vals)
        move.sales_rep_id = self.sales_rep_id.id
        move.discount_id = self.discount_reason_id.id
        move.order_number = self.tracking_number

        for line  in  move.invoice_line_ids :
            warranty = self.env['product.warranty'].search([('categ_ids', 'in', line.product_id.categ_id.id)])

            line.family_id = line.product_id.product_tmpl_id.family_id.id
            line.categ_id = line.product_id.categ_id.id
            line.product_upc = line.product_id.default_code
            line.item_code = line.product_id.item_code
            line.vendor_id = line.product_id.vendor_id
            line.standard_price = line.product_id.standard_price
            line.warranty_id = warranty if warranty else False

        # Auto print the invoice after creation

        return move

    @api.model
    def sync_from_ui(self, orders):
        data = super().sync_from_ui(orders)
        return data





class PosConfig(models.Model):
    _inherit = 'pos.config'

    auto_invoice = fields.Boolean(
        string='Auto Invoice',
        default=True,
        help='Automatically create invoices for all POS orders'
    )

    auto_print_invoice = fields.Boolean(
        string='Auto Print Invoice',
        default=True,
        help='Automatically print invoices after creation'
    )


class PosSession(models.Model):
    _inherit = 'pos.session'



    def _loader_params_pos_config(self):
        """Load pos.config fields"""
        params = super()._loader_params_pos_config()
        params['search_params']['fields'].extend(['auto_invoice', 'auto_print_invoice'])
        return params


    def _load_pos_data(self, data):
        """Load POS data and add `res_users` to the response dictionary.
        return: A dictionary containing the POS data.
        """
        data = super()._load_pos_data(data)
        data['data'][0]['sales_reps'] = self.env['sales.rep'].search_read(fields=['id', 'name'])
        data['data'][0]['discount_reason'] = self.env['discount.reason'].search_read(fields=['id', 'name','discount_percentage'])
        return data

    @api.model
    def _load_pos_data_models(self, config_id):
        data = super()._load_pos_data_models(config_id)
        data += ['sales.rep', 'discount.reason']
        return data

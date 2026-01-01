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
        # Auto print the invoice after creation
        if move and self.config_id.auto_print_invoice:
            try:
                # Print the invoice
                move.print_invoice()
                # You can also add logging here
                self.env['pos.session']._notify_changes_in_session()
            except Exception as e:
                # Log the error but don't stop the process
                import logging
                _logger = logging.getLogger(__name__)
                _logger.error(f"Failed to auto print invoice {move.name}: {str(e)}")

        return move

    @api.model
    def sync_from_ui(self, orders):
        print(orders)
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

# Part of BrowseInfo. See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.osv import expression
from pprint import pformat
from odoo.exceptions import ValidationError
import logging
from random import randrange
_logger = logging.getLogger(__name__)

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    allowed_branch = fields.Boolean(
        string='Allowed_branch',
        required=False)

    payment_ids = fields.One2many(
        'account.payment',
        'sale_order_id',
        string='Payments'
    )

    amount_paid = fields.Monetary(
        compute='_compute_amount_paid',
        store=True,
        currency_field='currency_id'
    )

    amount_due = fields.Monetary(
        compute='_compute_amount_paid',
        store=True,
        currency_field='currency_id'
    )

    fully_paid = fields.Boolean(
        compute='_compute_fully_paid',
        store=True
    )

    payment_count = fields.Integer(
        compute='_compute_payment_count'
    )

    def _compute_payment_count(self):
        for order in self:
            order.payment_count = self.env['account.payment'].search_count( [('sale_order_id', '=', self.id)])

    def action_view_payments(self):
        self.ensure_one()
        return {
            'name': ('Payments'),
            'type': 'ir.actions.act_window',
            'res_model': 'account.payment',
            'view_mode': 'list,form',
            'domain': [('sale_order_id', '=', self.id)],
            'context': {'default_sale_order_id': self.id},
        }
    def action_register_payment_so(self):
        self.ensure_one()

        if self.amount_total <= 0:
            raise UserError(("Nothing to pay."))

        return {
            'name': ('Register Payment'),
            'type': 'ir.actions.act_window',
            'res_model': 'account.payment',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_payment_type': 'inbound',
                'default_partner_type': 'customer',
                'default_partner_id': self.partner_id.id,
                'default_branch_id': self.branch_id.id,
                'default_amount': self.amount_total,
                'default_sale_order_id': self.id,
                'default_ref': self.name,
            }
        }


    @api.depends('amount_due')
    def _compute_fully_paid(self):
        for order in self:
            order.fully_paid = order.amount_due <= 0

    @api.depends(
        'payment_ids.state',
        'payment_ids.amount',
        'amount_total'
    )
    def _compute_amount_paid(self):
        for order in self:
            paid = sum(
                p.amount for p in order.payment_ids
                if p.state == 'paid'
            )
            order.amount_paid = paid
            order.amount_due = order.amount_total - paid


    @api.model_create_multi
    def default_get(self,fields):
        res = super(SaleOrder, self).default_get(fields)
        branch_id = warehouse_id =allowed_branch = False
        if self.env.user.branch_id :
            if self.env.user.branch_id.company_id.id == self.env.company.id:
                branch_id = self.env.user.branch_id.id
        if len(self.env.user.branch_ids)>1:
            allowed_branch =True
        if branch_id:
            branched_warehouse = self.env['stock.warehouse'].search([('branch_id','=',branch_id)])
            if branched_warehouse:
                warehouse_id = branched_warehouse.ids[0]
        
        if not warehouse_id:
            warehouse_id = self.env.user._get_default_warehouse_id()
            warehouse_id = warehouse_id.id

        res.update({
            'branch_id': branch_id,
            'warehouse_id': warehouse_id,
            'allowed_branch': allowed_branch
        })

        return res

    branch_id = fields.Many2one('res.branch', string="Branch",required=True)

    
    def _prepare_invoice(self):
        res = super(SaleOrder, self)._prepare_invoice()
        res['branch_id'] = self.branch_id.id
        return res

    @api.model_create_multi
    def create(self, values):
        # Add code here
        if self.env.user.id not in [1,2]:
            if not self.env.user.has_group('branch.group_allow_create_sales') :
                raise UserError("Not Allowed For Create Salse Order")

        return super(SaleOrder, self).create(values)

    #
    # @api.onchange('branch_id')
    # def _onchange_branch_id(self):
    #     selected_brach = self.branch_id
    #     if selected_brach:
    #         user_id = self.env['res.users'].browse(self.env.uid)
    #         user_branch = user_id.sudo().branch_id
    #         if user_branch and user_branch.id != selected_brach.id:
    #             raise UserError("Please select active branch only. Other may create the Multi branch issue. \n\ne.g: If you wish to add other branch then Switch branch from the header and set that.")

    # @api.model
    # def web_search_read(self, domain, specification, offset=0, limit=None, order=None, count_limit=None):
    #     if 'allowed_branch_ids' in self.env.context:
    #         domain = expression.AND([domain, [('branch_id', 'in', self.env.context.get('allowed_branch_ids'))]])
    #     return super().web_search_read(domain, specification, offset=offset, limit=limit, order=order, count_limit=count_limit)
class POSOrder(models.Model):
    _inherit = 'pos.order'


    branch_id = fields.Many2one('res.branch', string="Branch")

    @api.model
    def sync_from_ui(self, orders):
        """ Create and update Orders from the frontend PoS application.

        Create new orders and update orders that are in draft status. If an order already exists with a status
        different from 'draft' it will be discarded, otherwise it will be saved to the database. If saved with
        'draft' status the order can be overwritten later by this function.

        :param orders: dictionary with the orders to be created.
        :type orders: dict.
        :param draft: Indicate if the orders are meant to be finalized or temporarily saved.
        :type draft: bool.
        :Returns: list -- list of db-ids for the created and updated orders.
        """
        sync_token = randrange(100_000_000)  # Use to differentiate 2 parallels calls to this function in the logs
        _logger.info("PoS synchronisation #%d started for PoS orders references: %s", sync_token,
                     [self._get_order_log_representation(order) for order in orders])
        order_ids = []
        for order in orders:
            order_log_name = self._get_order_log_representation(order)
            _logger.debug("PoS synchronisation #%d processing order %s order full data: %s", sync_token, order_log_name,
                          pformat(order))

            if len(self._get_refunded_orders(order)) > 1:
                raise ValidationError(_('You can only refund products from the same order.'))

            existing_order = self._get_open_order(order)
            if existing_order and existing_order.state == 'draft':
                order_ids.append(self._process_order(order, existing_order))
                _logger.info("PoS synchronisation #%d order %s updated pos.order #%d", sync_token, order_log_name,
                             order_ids[-1])
            elif not existing_order:
                order_ids.append(self._process_order(order, False))
                _logger.info("PoS synchronisation #%d order %s created pos.order #%d", sync_token, order_log_name,
                             order_ids[-1])
            else:
                # In theory, this situation is unintended
                # In practice it can happen when "Tip later" option is used
                order_ids.append(existing_order.id)
                _logger.info("PoS synchronisation #%d order %s sync ignored for existing PoS order %s (state: %s)",
                             sync_token, order_log_name, existing_order, existing_order.state)

        # Sometime pos_orders_ids can be empty.
        pos_order_ids = self.env['pos.order'].browse(order_ids)
        config_id = pos_order_ids.config_id.ids[0] if pos_order_ids else False
        pos_order_ids.branch_id = self.env.user.branch_id.id
        for order in pos_order_ids:
            order._ensure_access_token()
            if not self.env.context.get('preparation'):
                order.config_id.notify_synchronisation(order.config_id.current_session_id.id,
                                                       self.env.context.get('login_number', 0))

        _logger.info("PoS synchronisation #%d finished", sync_token)
        return pos_order_ids.read_pos_data(orders, config_id)







class PosConfig(models.Model):
    _inherit = 'pos.config'

    branch_id = fields.Many2one('res.branch', string="Branch")

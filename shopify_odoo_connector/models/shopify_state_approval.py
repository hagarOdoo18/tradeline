# -*- coding: utf-8 -*-
"""Admin approval before the delivery or invoice of a Shopify order changes
state.

Applies only to stock.picking / account.move records linked to a sale order
that came from Shopify. A user without the Shopify / Manager group cannot
validate or cancel such a delivery, or post / reset to draft / cancel such an
invoice, until a Shopify Manager presses "Approve State Change". The approval
is consumed by the next state change, so every further change needs a new
approval.

Not gated (they run without asking):
  * Shopify Managers themselves - they are the approvers;
  * the superuser / sudo() - the webhooks (auth='none', with_user(SUPERUSER_ID))
    and the cancelled-orders cron;
  * code that passes the context key ``shopify_skip_state_approval`` - the
    automatic validate-and-invoice step after an order import.
"""
from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import AccessError, UserError

MANAGER_GROUP = 'shopify_odoo_connector.group_shopify_manager'
SKIP_KEY = 'shopify_skip_state_approval'
ACTIVITY_SUMMARY = 'Shopify: approve state change'


class ShopifyStateApprovalMixin(models.AbstractModel):
    _name = 'shopify.state.approval.mixin'
    _description = 'Shopify order document state-change approval'

    is_shopify_order = fields.Boolean(
        string='From Shopify Order', compute='_compute_is_shopify_order',
        help='Linked to a sale order imported from Shopify: state changes '
             'need a Shopify Manager approval first.')
    shopify_state_approved = fields.Boolean(
        string='State Change Approved', copy=False, readonly=True)
    shopify_approval_requested = fields.Boolean(
        string='Approval Requested', copy=False, readonly=True)
    shopify_approved_by_id = fields.Many2one(
        'res.users', string='Approved By', copy=False, readonly=True)
    shopify_approved_date = fields.Datetime(
        string='Approved On', copy=False, readonly=True)

    # ------------------------------------------------------------------ helpers
    def _shopify_sale_orders(self):
        """Shopify sale orders this record belongs to. Implemented per model."""
        return self.env['sale.order']

    @api.model
    def _is_shopify_sale_order(self, orders):
        return orders.filtered(lambda o: o.shopify_instance_id
                               or o.shopify_order_ref
                               or o.shopify_sync_ids)

    def _compute_is_shopify_order(self):
        for rec in self:
            rec.is_shopify_order = bool(rec._shopify_sale_orders())

    def _shopify_approval_bypass(self):
        env = self.env
        return bool(
            env.su
            or not env.uid
            or env.uid == SUPERUSER_ID
            or env.context.get(SKIP_KEY)
            or env.user.has_group(MANAGER_GROUP))

    def _shopify_check_state_approval(self, action_label):
        """Raise unless every Shopify record in self has been approved."""
        if self._shopify_approval_bypass():
            return
        blocked = self.filtered(
            lambda r: r.is_shopify_order and not r.shopify_state_approved)
        if blocked:
            raise UserError(_(
                '%(action)s needs a Shopify Manager approval first because '
                'it belongs to a Shopify order:\n%(docs)s\n\nUse "Request '
                'Approval" on the document and wait for the approval.',
                action=action_label,
                docs='\n'.join('- %s' % n for n in blocked.mapped(
                    'display_name'))))

    def _shopify_run_gated(self, action_label, method):
        """Check approval, run ``method`` (a callable taking no argument),
        then consume the approval on every record whose state changed."""
        self._shopify_check_state_approval(action_label)
        before = {rec.id: rec.state for rec in self}
        res = method()
        changed = self.filtered(
            lambda r: r.shopify_state_approved
            and r.state != before.get(r.id))
        if changed:
            changed.sudo().write({
                'shopify_state_approved': False,
                'shopify_approval_requested': False,
                'shopify_approved_by_id': False,
                'shopify_approved_date': False,
            })
        return res

    def _shopify_approval_activities(self):
        return self.activity_ids.filtered(
            lambda a: a.summary == ACTIVITY_SUMMARY)

    # ------------------------------------------------------------------ buttons
    def action_shopify_request_approval(self):
        managers = self.env['res.users'].sudo().search([
            ('groups_id', 'in', self.env.ref(MANAGER_GROUP).id),
            ('share', '=', False),
            ('id', '!=', SUPERUSER_ID),
        ])
        for rec in self.filtered(lambda r: r.is_shopify_order
                                 and not r.shopify_state_approved):
            rec_sudo = rec.sudo()
            for manager in managers:
                rec_sudo.activity_schedule(
                    'mail.mail_activity_data_todo',
                    user_id=manager.id,
                    summary=ACTIVITY_SUMMARY,
                    note=_('%(user)s asks to change the state of %(doc)s '
                           '(Shopify order).', user=self.env.user.name,
                           doc=rec.display_name))
            rec_sudo.shopify_approval_requested = True
            rec_sudo.message_post(
                body=_('State change approval requested by %s.')
                % self.env.user.name,
                subtype_xmlid='mail.mt_note')
        return True

    def action_shopify_approve_state_change(self):
        if not self.env.user.has_group(MANAGER_GROUP):
            raise AccessError(_('Only a Shopify Manager can approve.'))
        for rec in self.filtered('is_shopify_order').sudo():
            rec.write({
                'shopify_state_approved': True,
                'shopify_approved_by_id': self.env.uid,
                'shopify_approved_date': fields.Datetime.now(),
            })
            rec._shopify_approval_activities().action_feedback(
                feedback=_('Approved by %s') % self.env.user.name)
            rec.message_post(
                body=_('State change approved by %s (valid for the next '
                       'state change only).') % self.env.user.name,
                subtype_xmlid='mail.mt_note')
        return True


class StockPicking(models.Model):
    _name = 'stock.picking'
    _inherit = ['stock.picking', 'shopify.state.approval.mixin']

    @api.depends('sale_id')
    def _compute_is_shopify_order(self):
        return super()._compute_is_shopify_order()

    def _shopify_sale_orders(self):
        return self._is_shopify_sale_order(self.sale_id)

    def button_validate(self):
        return self._shopify_run_gated(
            _('Validating the delivery'),
            lambda: super(StockPicking, self).button_validate())

    def action_cancel(self):
        return self._shopify_run_gated(
            _('Cancelling the delivery'),
            lambda: super(StockPicking, self).action_cancel())


class AccountMove(models.Model):
    _name = 'account.move'
    _inherit = ['account.move', 'shopify.state.approval.mixin']

    @api.depends('line_ids.sale_line_ids', 'reversed_entry_id')
    def _compute_is_shopify_order(self):
        return super()._compute_is_shopify_order()

    def _shopify_sale_orders(self):
        orders = self.line_ids.sale_line_ids.order_id
        orders |= self.reversed_entry_id.line_ids.sale_line_ids.order_id
        return self._is_shopify_sale_order(orders)

    def action_post(self):
        return self._shopify_run_gated(
            _('Posting the invoice'),
            lambda: super(AccountMove, self).action_post())

    def button_draft(self):
        return self._shopify_run_gated(
            _('Resetting the invoice to draft'),
            lambda: super(AccountMove, self).button_draft())

    def button_cancel(self):
        return self._shopify_run_gated(
            _('Cancelling the invoice'),
            lambda: super(AccountMove, self).button_cancel())


class ValidateAccountMove(models.TransientModel):
    """Accounting > "Confirm Entries" (list action) posts through this wizard
    with _post(), not action_post(): gate it the same way."""
    _inherit = 'validate.account.move'

    def validate_move(self):
        return self.move_ids._shopify_run_gated(
            _('Posting the invoice'),
            lambda: super(ValidateAccountMove, self).validate_move())

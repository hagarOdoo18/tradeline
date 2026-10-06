# -*- coding: utf-8 -*-
################################################################################
#
#    Cybrosys Technologies Pvt. Ltd.
#
#    Copyright (C) 2025-TODAY Cybrosys Technologies(<https://www.cybrosys.com>).
#    Author: Cybrosys Techno Solutions (Contact : odoo@cybrosys.com)
#
#    This program is under the terms of the Odoo Proprietary License v1.0
#    (OPL-1)
#    It is forbidden to publish, distribute, sublicense, or sell copies of the
#    Software or modified copies of the Software.
#
#    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
#    IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
#    DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
#    OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE
#    USE OR OTHER DEALINGS IN THE SOFTWARE.
#
################################################################################
import json
import logging
import requests
from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_compare

_logger = logging.getLogger(__name__)


class SaleOrder(models.Model):
    """Class for inherited model sale. order

        Methods:
            def sync_shopify_order(self):
                Method to sync odoo orders into shopify.
            action_confirm(self):
                Supering the action_confirm function inorder to confirm the
                created sale order.
    """
    _inherit = 'sale.order'

    shopify_instance_id = fields.Many2one('shopify.configuration',
                                          string="Shopify Instance",
                                          help='Shopify instance id of '
                                               'sale order.')
    shopify_sync_ids = fields.One2many('shopify.sync',
                                       'order_id',
                                       string='Shopify sync',
                                       help='Shopify sync ida of sale order.')
    shopify_order_ref = fields.Char(string='Shopify Order Id',
                                    help='Shopify id of order')

    def _shopify_branch_user(self):
        """User that receives the Shopify order mail for this order's
        branch - same rule the import uses for user_id: branch 88 -> user
        66, otherwise the user whose Branch is the order's branch.
        Returns a res.users record (possibly empty), never an id."""
        self.ensure_one()
        users = self.env['res.users'].sudo()
        branch = self.branch_id or self.warehouse_id.branch_id
        if not branch:
            return users
        if branch.id == 88:
            return users.browse(66).exists()
        # res.users.branch_id is company_dependent -> search in the
        # order's company; limit=1 avoids a singleton error when several
        # users share the branch
        return users.with_company(self.company_id).search(
            [('branch_id', '=', branch.id)], limit=1)

    def _shopify_notify_branch(self):
        """Tell the branch the Shopify order was created at.

        Recipient = _shopify_branch_user() (same rule as the import). The
        user gets:
          * an Odoo inbox notification linked to the order,
          * a sticky pop-up on screen if logged in,
          * the email (only queued - leaves with the mail cron).
        Bus pop-ups and mails go out only after the transaction commits,
        so an order rolled back later notifies nobody. Never raises - a
        notification problem must not undo an imported order."""
        template = self.env.ref(
            'shopify_odoo_connector.mail_template_shopify_branch_order_v1',
            raise_if_not_found=False)
        for order in self.sudo():
            branch = order.branch_id or order.warehouse_id.branch_id
            user = order._shopify_branch_user()
            if not user:
                order._shopify_log_branch_notify(
                    'branch notification not sent - branch "%s" has no '
                    'user.' % (branch.name or '-'))
                continue
            order._shopify_notify_branch_in_app(user, branch)
            if not template:
                continue
            if not user.email:
                order._shopify_log_branch_notify(
                    'branch email not sent - user "%s" of branch "%s" has '
                    'no email.' % (user.name, branch.name or '-'))
                continue
            try:
                with self.env.cr.savepoint():
                    template.send_mail(
                        order.id, force_send=False,
                        email_values={'recipient_ids': [
                            (6, 0, user.partner_id.ids)]})
                    order.message_post(
                        body='Shopify order notification queued to branch '
                             '%s (%s).' % (branch.name, user.email),
                        subtype_xmlid='mail.mt_note')
            except Exception as error:
                _logger.exception('Shopify branch mail failed for %s',
                                  order.name)
                order._shopify_log_branch_notify(
                    'branch email failed - %s' % error)

    def _shopify_log_branch_notify(self, message):
        self.ensure_one()
        self.env['log.message'].sudo().create({
            'name': 'Order %s: %s' % (
                self.reference_number or self.name, message),
            'shopify_instance_id': self.shopify_instance_id.id,
            'model': 'sale.order',
        })

    def _shopify_notify_branch_in_app(self, user, branch):
        """Odoo inbox notification + live pop-up for the branch user."""
        self.ensure_one()
        title = 'New Shopify order %s' % (self.reference_number or self.name)
        text = '%s - %s - %s %s (branch %s)' % (
            self.name, self.partner_id.name or '',
            self.amount_total, self.currency_id.symbol or '',
            branch.name or '-')
        try:
            with self.env.cr.savepoint():
                # inbox (bell icon) - or an email if the user chose
                # "Handle by Emails" in his preferences
                self.message_notify(
                    partner_ids=user.partner_id.ids,
                    subject=title,
                    body=text,
                    record_name=self.name,
                    email_layout_xmlid='mail.mail_notification_light',
                )
                # sticky pop-up, delivered after commit to the user's open
                # Odoo tabs
                user.partner_id._bus_send('simple_notification', {
                    'type': 'info',
                    'title': title,
                    'message': text,
                    'sticky': True,
                })
        except Exception as error:
            _logger.exception('Shopify branch in-app notification failed '
                              'for %s', self.name)
            self._shopify_log_branch_notify(
                'branch in-app notification failed - %s' % error)

    # ------------------------------------------------------------------
    # Delivery + invoice right after a Shopify order is imported
    # ------------------------------------------------------------------
    def _shopify_log(self, message):
        """Write a log.message and a chatter note for this order."""
        self.ensure_one()
        self.env['log.message'].sudo().create({
            'name': 'Order %s: %s' % (
                self.reference_number or self.name, message),
            'shopify_instance_id': self.shopify_instance_id.id,
            'model': 'sale.order',
        })
        self.sudo().message_post(body=message, subtype_xmlid='mail.mt_note')

    def _shopify_validate_deliveries(self):
        """Validate every open delivery of this order with the reserved
        quantities (lots / serials come from the reservation).

        A picking is validated only when ALL its moves are fully reserved -
        quantities are never forced, so stock_no_negative and serial
        tracking stay respected. Multi-step routes (pick -> out) open the
        next picking only once the previous one is done, hence the loop.
        Raises UserError with the reason when something cannot be
        delivered; the caller rolls back to its savepoint."""
        self.ensure_one()
        order = self.with_context(
            skip_shopify_write=True, skip_sms=True,
            skip_backorder=True, cancel_backorder=False,
            skip_immediate=True,
            # automatic step: no Shopify Manager approval needed
            shopify_skip_state_approval=True)
        for _step in range(5):
            pickings = order.picking_ids.filtered(
                lambda p: p.state not in ('done', 'cancel')
                and p.picking_type_code != 'incoming')
            if not pickings:
                return True
            pickings.action_assign()
            ready = pickings.filtered(lambda p: p.move_ids.filtered(
                lambda m: m.state != 'cancel') and all(
                m.state == 'assigned' for m in p.move_ids
                if m.state != 'cancel'))
            if not ready:
                missing = []
                for move in pickings.move_ids.filtered(
                        lambda m: m.state not in ('assigned', 'done',
                                                  'cancel')):
                    missing.append('%s (needed %s, reserved %s)' % (
                        move.product_id.display_name, move.product_uom_qty,
                        move.quantity))
                raise UserError(
                    'not enough stock reserved in %s: %s' % (
                        ', '.join(pickings.mapped('name')),
                        '; '.join(missing) or 'waiting on another operation'))
            for picking in ready:
                for move in picking.move_ids.filtered(
                        lambda m: m.state != 'cancel'):
                    if float_compare(
                            move.quantity, move.product_uom_qty,
                            precision_rounding=move.product_uom.rounding) < 0:
                        raise UserError('%s is not fully reserved in %s' % (
                            move.product_id.display_name, picking.name))
                    move.picked = True
                picking.button_validate()
                if picking.state != 'done':
                    raise UserError(
                        'picking %s could not be validated (state: %s)' % (
                            picking.name, picking.state))
        raise UserError('deliveries still open after 5 validation rounds')

    def _shopify_create_invoice(self):
        """Create and post the customer invoice for what was delivered."""
        self.ensure_one()
        order = self.with_context(skip_shopify_write=True,
                                  shopify_skip_state_approval=True)
        if order.invoice_status != 'to invoice':
            return order.env['account.move']
        invoices = order._create_invoices()
        drafts = invoices.filtered(lambda move: move.state == 'draft')
        if drafts:
            drafts.action_post()
        return invoices

    def _shopify_deliver_and_invoice(self):
        """Validate the delivery, then create + post the invoice, for
        orders imported from Shopify.

        Each step runs in its own savepoint and never raises: the order is
        already imported and confirmed, so a missing serial or an empty
        stock only leaves that step undone and writes a log.message - it
        must not roll back (or count as failed) the imported order. No
        invoice is made while the delivery is still open."""
        for order in self:
            order = order.with_company(order.company_id)
            if order.state != 'sale':
                continue
            try:
                with self.env.cr.savepoint():
                    order._shopify_validate_deliveries()
            except Exception as error:
                self.env.invalidate_all()
                _logger.warning('Shopify order %s: delivery not validated: '
                                '%s', order.name, error)
                order._shopify_log(
                    'delivery not validated, no invoice created - %s' % error)
                continue
            try:
                with self.env.cr.savepoint():
                    invoices = order._shopify_create_invoice()
            except Exception as error:
                self.env.invalidate_all()
                _logger.warning('Shopify order %s: invoice not created: %s',
                                order.name, error)
                order._shopify_log(
                    'delivered, but invoice not created - %s' % error)
                continue
            if invoices:
                order.sudo().message_post(
                    body='Delivery validated and invoice %s posted '
                         'automatically.' % ', '.join(
                             invoices.mapped('name')),
                    subtype_xmlid='mail.mt_note')

    # ------------------------------------------------------------------
    # Cancellation coming from Shopify: return + credit note, then cancel
    # ------------------------------------------------------------------
    def _shopify_cancel_context(self):
        return self.with_company(self.company_id).with_context(
            skip_shopify_write=True, disable_cancel_warning=True,
            skip_sms=True, skip_backorder=True, cancel_backorder=False,
            skip_immediate=True,
            # automatic step: no Shopify Manager approval needed
            shopify_skip_state_approval=True)

    @staticmethod
    def _shopify_returnable_qty(move):
        """Quantity of a done move not returned yet (open returns count)."""
        returned = sum(move.returned_move_ids.filtered(
            lambda m: m.state != 'cancel').mapped('product_qty'))
        return move.product_qty - returned

    def _shopify_fill_return_lots(self, return_picking):
        """Copy the lots / serials of the delivered move lines onto the
        return moves, so a tracked product can be validated."""
        for move in return_picking.move_ids.filtered(
                lambda m: m.state not in ('done', 'cancel')
                and m.product_id.tracking != 'none'):
            if move.move_line_ids and all(
                    ml.lot_id for ml in move.move_line_ids):
                continue
            origin_lines = move.origin_returned_move_id.move_line_ids.filtered(
                'lot_id')
            if not origin_lines:
                continue
            move.move_line_ids.unlink()
            remaining = move.product_uom_qty
            vals_list = []
            for line in origin_lines:
                if remaining <= 0:
                    break
                qty = min(line.quantity, remaining)
                remaining -= qty
                vals_list.append({
                    'move_id': move.id,
                    'picking_id': return_picking.id,
                    'product_id': move.product_id.id,
                    'product_uom_id': move.product_uom.id,
                    'lot_id': line.lot_id.id,
                    'quantity': qty,
                    'location_id': move.location_id.id,
                    'location_dest_id': move.location_dest_id.id,
                })
            self.env['stock.move.line'].create(vals_list)

    def _shopify_return_deliveries(self):
        """Create and validate a return for every done delivery of the
        order that still has something left to return.

            stock.picking: the validated return pickings.
        Raises UserError when a return cannot be validated."""
        self.ensure_one()
        order = self._shopify_cancel_context()
        returns = self.env['stock.picking']
        deliveries = order.picking_ids.filtered(
            lambda p: p.state == 'done' and p.picking_type_code == 'outgoing'
            and not p.move_ids.origin_returned_move_id)
        for picking in deliveries:
            wizard = order.env['stock.return.picking'].with_context(
                dict(order.env.context, active_id=picking.id,
                     active_ids=picking.ids, active_model='stock.picking')
            ).create({'picking_id': picking.id})
            to_return = False
            for line in wizard.product_return_moves:
                qty = self._shopify_returnable_qty(line.move_id) \
                    if line.move_id else 0.0
                line.quantity = max(qty, 0.0)
                to_return = to_return or qty > 0
            # nothing left to return (already returned earlier)
            wizard.product_return_moves.filtered(
                lambda l: l.quantity <= 0).unlink()
            if not to_return or not wizard.product_return_moves:
                continue
            if hasattr(wizard, '_create_return'):
                return_picking = wizard._create_return()
            else:  # Odoo <= 16
                return_picking = self.env['stock.picking'].browse(
                    wizard._create_returns()[0])
            return_picking = return_picking.with_context(order.env.context)
            if return_picking.state == 'draft':
                return_picking.action_confirm()
            return_picking.action_assign()
            self._shopify_fill_return_lots(return_picking)
            for move in return_picking.move_ids.filtered(
                    lambda m: m.state not in ('done', 'cancel')):
                if float_compare(
                        move.quantity, move.product_uom_qty,
                        precision_rounding=move.product_uom.rounding) < 0:
                    move.quantity = move.product_uom_qty
                move.picked = True
            return_picking.button_validate()
            if return_picking.state != 'done':
                raise UserError(
                    'return %s of %s could not be validated (state: %s)' % (
                        return_picking.name, picking.name,
                        return_picking.state))
            returns |= return_picking
        return returns

    def _shopify_refund_invoices(self):
        """Create and post a full credit note for every posted customer
        invoice of the order that is not reversed yet. An unpaid invoice is
        reconciled with its credit note; a paid one keeps the credit note
        open as a customer credit to refund.

            account.move: the posted credit notes."""
        self.ensure_one()
        order = self._shopify_cancel_context()
        today = fields.Date.context_today(self)
        credit_notes = self.env['account.move']
        invoices = order.invoice_ids.filtered(
            lambda m: m.move_type == 'out_invoice' and m.state == 'posted'
            and m.payment_state != 'reversed')
        for invoice in invoices:
            reversals = getattr(invoice, 'reversal_move_ids', None)
            if reversals is None:
                reversals = getattr(invoice, 'reversal_move_id',
                                    self.env['account.move'])
            if reversals.filtered(lambda m: m.state == 'posted'):
                continue
            unpaid = invoice.payment_state == 'not_paid'
            refund = invoice.with_context(order.env.context)._reverse_moves(
                [{
                    'ref': 'Reversal of %s - Shopify order %s cancelled' % (
                        invoice.name, order.reference_number or order.name),
                    'invoice_date': today,
                    'date': today,
                }],
                cancel=unpaid)
            drafts = refund.filtered(lambda m: m.state == 'draft')
            if drafts:
                drafts.action_post()
            credit_notes |= refund
        return credit_notes

    def _shopify_cancel_from_shopify(self, reason=None):
        """Cancel an order that was cancelled in Shopify.

        Delivered goods are returned (validated return picking) and posted
        invoices get a credit note before the order itself is cancelled.
        All or nothing: raises UserError on the first failure, the caller
        rolls back to its savepoint.

            dict: ``returns`` / ``credit_notes`` / ``cancelled_pickings``.
        """
        self.ensure_one()
        order = self._shopify_cancel_context()
        result = {
            'returns': self.env['stock.picking'],
            'credit_notes': self.env['account.move'],
            'cancelled_pickings': self.env['stock.picking'],
        }
        if order.state == 'cancel':
            return result
        result['returns'] = order._shopify_return_deliveries()
        result['credit_notes'] = order._shopify_refund_invoices()

        open_pickings = order.picking_ids.filtered(
            lambda p: p.state not in ('done', 'cancel'))
        if getattr(order, 'locked', False):
            order.action_unlock()
        order.action_cancel()
        if order.state != 'cancel':
            raise UserError('Odoo did not cancel the order (state is still '
                            '"%s").' % order.state)
        result['cancelled_pickings'] = open_pickings.filtered(
            lambda p: p.state == 'cancel')

        parts = ['Cancelled from Shopify']
        if reason:
            parts.append('reason: %s' % reason)
        if result['returns']:
            parts.append('return %s validated' % ', '.join(
                result['returns'].mapped('name')))
        if result['credit_notes']:
            parts.append('credit note %s posted' % ', '.join(
                result['credit_notes'].mapped('name')))
        order._shopify_log(' - '.join(parts) + '.')
        return result

    @api.model
    def _cron_cancel_shopify_orders(self):
        """Scheduled action to cancel Odoo sale orders whose Shopify
        counterpart has been cancelled. For each active connected instance the
        Shopify cancelled orders are fetched and the matching Odoo order (by
        Shopify order ref) is cancelled if it is not already cancelled."""
        instances = self.env['shopify.configuration'].search(
                    [('company_id', '=', self.env.company.id)])
        for instance in instances:
            try:
                self._cancel_shopify_orders_for_instance(instance)
            except Exception as error:
                _logger.error(
                    'Failed to sync cancelled orders for Shopify instance '
                    '%s: %s', instance.name, str(error))

    def _cancel_shopify_orders_for_instance(self, instance):
        """Fetch cancelled Shopify orders for one instance and cancel the
        matching Odoo sale orders."""
        store_name = instance.shop_name
        version = instance.version
        headers = instance._get_shopify_headers()
        next_url = ("https://%s/admin/api/%s/orders.json"
                    "?status=cancelled&limit=250") % (store_name, version)
        while next_url:
            response = requests.request(
                'GET', next_url, headers=headers, data=[], timeout=30)
            response.raise_for_status()
            response_json = response.json()
            for each in response_json.get('orders', []):
                if not each.get('cancelled_at'):
                    continue
                sync = self.env['shopify.sync'].sudo().search([
                    ('instance_id', '=', instance.id),
                    ('shopify_order_ref', '=', str(each['id'])),
                    ('order_id', '!=', False),
                ], limit=1)
                order = sync.order_id
                if order and order.state != 'cancel':
                    try:
                        with self.env.cr.savepoint():
                            order.sudo()._shopify_cancel_from_shopify(
                                each.get('cancel_reason'))
                    except Exception as error:
                        self.env.invalidate_all()
                        order.sudo()._shopify_log(
                            'Shopify cancellation not applied - %s' % error)
                        _logger.error(
                            'Failed to cancel Odoo order %s for cancelled '
                            'Shopify order %s: %s',
                            order.name, each['id'], str(error))
            # Parse next page URL from the Link header
            next_url = None
            link_header = response.headers.get('link', '')
            for part in link_header.split(','):
                part = part.strip()
                if 'rel="next"' in part:
                    start = part.find('<') + 1
                    end = part.find('>')
                    if start > 0 and end > start:
                        next_url = part[start:end]
                    break

    def sync_shopify_order(self):
        """Method to sync odoo orders into shopify"""
        instance = self.shopify_instance_id
        store_name = instance.shop_name
        version = instance.version
        order_url = "https://%s/admin/api/%s/draft_orders.json" % (
            store_name, version)
        instance_ids = self.shopify_sync_ids.mapped('instance_id.id')
        if instance.id not in instance_ids:
            line_items = []
            for line in self.order_line:
                line_vals = {
                    "title": line.product_id.name,
                    "price": line.price_unit,
                    "quantity": int(line.product_uom_qty),
                }
                line_items.append(line_vals)
            payload = json.dumps({
                "draft_order": {
                    "line_items": line_items,
                    "email": self.partner_id.email,
                    "use_customer_default_address": True
                }
            })
            response = requests.request("POST", order_url,
                                        headers=instance._get_shopify_headers(),
                                        data=payload)
            response_rec = response.json()
            if response_rec.get('draft_order'):
                response_order_id = response_rec['draft_order']['id']
                response_status = response_rec['draft_order']['status']
                response_name = response_rec['draft_order']['name']
                self.shopify_sync_ids.sudo().create({
                    'instance_id': instance.id,
                    'shopify_order_ref': response_order_id,
                    'shopify_order_name': response_name,
                    'shopify_order_number': response_order_id,
                    'order_status': response_status,
                    'order_id': self.id,
                    'synced_order': True,
                })
                self.shopify_order_ref = response_order_id

    def action_confirm(self):
        """Supering the action_confirm function inorder to confirm the created
           sale order.
           boolean: returns true or false
        """
        res = super(SaleOrder, self).action_confirm()
        # An order that came FROM Shopify must not be pushed back: its id is
        # a real order id, not a draft order's, so completing a "draft order"
        # with it either 404s or hits an unrelated draft order. The importer
        # and the confirmed-order API both set this flag.
        if self._context.get('skip_shopify_write'):
            return res
        # self may hold several orders (confirming from a list view, or a
        # batch import); reading self.shopify_order_ref directly raised
        # "Expected singleton" and aborted the whole confirmation.
        for order in self:
            if not (order.shopify_order_ref and order.shopify_instance_id):
                continue
            instance = order.shopify_instance_id
            store_name = instance.shop_name
            version = instance.version
            order_complete_url = ("https://%s/admin/api/%s/draft_orders/"
                                  "%s/complete.json") % (
                store_name, version, order.shopify_order_ref)
            line_items = []
            for line in order.order_line:
                line_vals = {
                    "title": line.product_id.name,
                    "price": line.price_unit,
                    "quantity": int(line.product_uom_qty),
                }
                line_items.append(line_vals)
            payload = json.dumps({
                "draft_order": {"line_items": line_items,
                                "email": order.partner_id.email,
                                "id": order.shopify_order_ref,
                                "status": "completed",
                                "use_customer_default_address": True}})
            requests.request("PUT", order_complete_url,
                             headers=instance._get_shopify_headers(),
                             data=payload)
        return res

    def write(self, vals):
        super().write(vals)
        for rec in self:
            if self._context.get('skip_shopify_write'):
                return True
            for config in self.env['shopify.configuration'].search(
                    [('company_id', '=', self.env.company.id)]):
                if rec.shopify_sync_ids.shopify_order_ref:
                    order_url = ("https://%s/admin/api/%s/draft_orders/%s.json") % (
                        config.shop_name, config.version,
                        rec.shopify_sync_ids.shopify_order_ref)
                    line_items = [{
                        'id': rec.shopify_sync_ids.shopify_order_ref,
                        "title": line.product_id.name,
                        "price": line.price_unit,
                        "quantity": int(line.product_uom_qty),
                    } for line in rec.order_line]
                    payload = json.dumps({
                        "draft_order": {
                            'id': rec.shopify_sync_ids.shopify_order_ref,
                            "line_items": line_items,
                            "email": rec.partner_id.email
                        }
                    })
                    requests.request("PUT", order_url,
                                     headers=config._get_shopify_headers(),
                                     data=payload)

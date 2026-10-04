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
            skip_immediate=True)
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
        order = self.with_context(skip_shopify_write=True)
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
                        order.sudo().with_context(
                            skip_shopify_write=True,
                            disable_cancel_warning=True,
                        ).action_cancel()
                    except Exception as error:
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

# -*- coding: utf-8 -*-
"""Durable, idempotent processing for Shopify order events."""

import hashlib
import json
import logging
import re
from datetime import datetime, timezone

import requests

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


_logger = logging.getLogger(__name__)
SHOPIFY_LOOKUP_TIMEOUT = 10
INVENTORY_BATCH_SIZE = 20


class ShopifyOrderEvent(models.Model):
    _name = 'shopify.order.event'
    _description = 'Shopify Order Event'
    _order = 'received_at desc, id desc'

    event_id = fields.Char(required=True, readonly=True, index=True)
    topic = fields.Char(required=True, readonly=True, index=True)
    instance_id = fields.Many2one(
        'shopify.configuration', required=True, readonly=True,
        ondelete='cascade', index=True)
    company_id = fields.Many2one(
        related='instance_id.company_id', store=True, readonly=True,
        index=True)
    shopify_order_id = fields.Char(readonly=True, index=True)
    shopify_order_name = fields.Char(readonly=True, index=True)
    payload = fields.Json(readonly=True)
    payload_hash = fields.Char(readonly=True)
    state = fields.Selection([
        ('received', 'Received'),
        ('processing', 'Processing'),
        ('done', 'Reserved'),
        ('duplicate', 'Duplicate'),
        ('blocked', 'Needs Attention'),
        ('failed', 'Failed'),
    ], required=True, default='received', readonly=True, index=True)
    attempts = fields.Integer(default=0, readonly=True)
    received_at = fields.Datetime(default=fields.Datetime.now, readonly=True)
    processed_at = fields.Datetime(readonly=True)
    last_attempt_at = fields.Datetime(readonly=True)
    error_message = fields.Text(readonly=True)
    order_id = fields.Many2one('sale.order', readonly=True, index=True)
    warehouse_id = fields.Many2one('stock.warehouse', readonly=True)
    warehouse_source = fields.Selection([
        ('payload', 'Shopify Payload'),
        ('fulfillment_order', 'Shopify Fulfillment Order'),
        ('default', 'Instance Default'),
    ], readonly=True)
    reservation_state = fields.Selection([
        ('reserved', 'Fully Reserved'),
        ('partial', 'Partially Reserved'),
        ('waiting', 'Waiting for Stock'),
        ('not_applicable', 'No Stockable Lines'),
    ], readonly=True)

    _sql_constraints = [
        ('shopify_order_event_unique',
         'unique(instance_id, event_id)',
         'This Shopify event has already been received.'),
    ]

    @api.model
    def _canonical_domain(self, value):
        value = (value or '').strip().lower()
        value = re.sub(r'^https?://', '', value).split('/', 1)[0]
        return value.rstrip('.')

    @api.model
    def find_instance(self, shop_domain):
        domain = self._canonical_domain(shop_domain)
        if not domain:
            return self.env['shopify.configuration'].browse()
        instances = self.env['shopify.configuration'].sudo().search([
            ('active', '=', True),
            ('state', '=', 'sync'),
            ('order_api_key', '!=', False),
        ])
        return instances.filtered(
            lambda rec: self._canonical_domain(rec.shop_name) == domain)[:1]

    @api.model
    def _numeric_id(self, value):
        if value in (None, False, ''):
            return ''
        return str(value).strip().rsplit('/', 1)[-1]

    @api.model
    def _event_identity(self, instance, topic, order_id, supplied_event_id):
        supplied = (supplied_event_id or '').strip()
        if supplied:
            return supplied[:255]
        if not order_id:
            raise ValueError('order.id is required')
        seed = '%s|%s|%s' % (instance.id, topic, order_id)
        return 'derived-' + hashlib.sha256(seed.encode()).hexdigest()

    @api.model
    def accept_and_process(self, instance, payload, topic='order.created',
                           supplied_event_id=''):
        if topic not in ('order.created', 'orders/create'):
            raise ValueError('unsupported_event_type')
        order_payload = payload.get('order', payload)
        if not isinstance(order_payload, dict):
            raise ValueError('order must be a JSON object')
        order_id = self._numeric_id(order_payload.get('id'))
        event_id = self._event_identity(
            instance, topic, order_id, supplied_event_id)

        # Serialize competing deliveries of the same event. Shopify Flow can
        # retry while the first request is still running.
        self.env.cr.execute(
            'SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
            ('shopify-order:%s:%s' % (instance.id, order_id),))
        existing = self.search([
            ('instance_id', '=', instance.id),
            ('event_id', '=', event_id),
        ], limit=1)
        if existing:
            if existing.state in ('failed', 'blocked', 'received'):
                existing._process()
            return existing, False

        canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'),
                               ensure_ascii=False)
        event = self.create({
            'event_id': event_id,
            'topic': topic,
            'instance_id': instance.id,
            'shopify_order_id': order_id,
            'shopify_order_name': order_payload.get('name'),
            'payload': payload,
            'payload_hash': hashlib.sha256(
                canonical.encode('utf-8')).hexdigest(),
        })
        event._process()
        return event, True

    def _find_order(self):
        self.ensure_one()
        order = self.env['sale.order'].sudo().search([
            ('shopify_instance_id', '=', self.instance_id.id),
            ('shopify_order_ref', '=', self.shopify_order_id),
        ], limit=1)
        if not order:
            sync = self.env['shopify.sync'].sudo().search([
                ('instance_id', '=', self.instance_id.id),
                ('shopify_order_ref', '=', self.shopify_order_id),
                ('order_id', '!=', False),
            ], limit=1)
            order = sync.order_id
        return order

    def _location_from_payload(self, order_payload):
        candidates = [
            order_payload.get('assigned_location_id'),
            order_payload.get('fulfillment_location_id'),
            order_payload.get('location_id'),
        ]
        location_keys = {
            'location', 'location_id', 'shopify_location',
            'shopify_location_id', 'fulfillment_location',
        }
        for attribute in order_payload.get('note_attributes') or []:
            if str(attribute.get('name') or '').strip().lower() in location_keys:
                candidates.append(attribute.get('value'))
        for candidate in candidates:
            if candidate not in (None, False, ''):
                return str(candidate).strip()
        return ''

    def _mapped_location(self, value):
        self.ensure_one()
        if not value:
            return self.env['shopify.location'].browse()
        numeric = self._numeric_id(value)
        return self.env['shopify.location'].sudo().search([
            ('instance_id', '=', self.instance_id.id),
            ('active', '=', True),
            '|',
            ('shopify_location_id', '=', numeric),
            ('name', '=ilike', str(value).strip()),
        ], limit=1)

    def _fulfillment_location(self):
        """Ask Shopify which single location owns this order, if assigned."""
        self.ensure_one()
        instance = self.instance_id
        url = 'https://%s/admin/api/%s/orders/%s/fulfillment_orders.json' % (
            instance.shop_name, instance.version, self.shopify_order_id)
        try:
            response = requests.get(
                url, headers=instance._get_shopify_headers(),
                timeout=SHOPIFY_LOOKUP_TIMEOUT)
            if response.status_code in (403, 404):
                _logger.info(
                    'No fulfillment-order location available for Shopify '
                    'order %s (HTTP %s).', self.shopify_order_id,
                    response.status_code)
                return self.env['shopify.location'].browse()
            response.raise_for_status()
            location_ids = {
                self._numeric_id(item.get('assigned_location_id'))
                for item in response.json().get('fulfillment_orders', [])
                if item.get('status') not in ('cancelled', 'closed') and
                item.get('assigned_location_id')
            }
        except (requests.RequestException, ValueError):
            _logger.exception(
                'Could not resolve the Shopify fulfillment location for '
                'order %s; using the configured default warehouse.',
                self.shopify_order_id)
            return self.env['shopify.location'].browse()
        if len(location_ids) > 1:
            raise ValidationError(_(
                'Shopify split this order across multiple locations. '
                'Automatic import is blocked until split fulfillment is '
                'configured.'))
        if not location_ids:
            return self.env['shopify.location'].browse()
        location_id = next(iter(location_ids))
        location = self._mapped_location(location_id)
        if not location or not location.warehouse_id:
            raise ValidationError(_(
                'Shopify location %s is not mapped to an Odoo warehouse.') %
                location_id)
        return location

    def _fetch_canonical_order(self):
        """Read the complete order from Shopify with Odoo's app token.

        Shopify Flow is the low-latency notification, not the accounting
        source.  Reading the canonical REST order here fills fields Flow can
        omit (taxes, discounts, billing address, product ids and refunds) and
        prevents a hand-edited Flow template from changing order values.
        """
        self.ensure_one()
        instance = self.instance_id
        url = 'https://%s/admin/api/%s/orders/%s.json' % (
            instance.shop_name, instance.version, self.shopify_order_id)
        try:
            response = requests.get(
                url, headers=instance._get_shopify_headers(),
                timeout=SHOPIFY_LOOKUP_TIMEOUT)
            response.raise_for_status()
            order = response.json().get('order')
        except (requests.RequestException, ValueError) as error:
            raise ValidationError(_(
                'Odoo could not read the canonical order from Shopify: %s') %
                                  str(error))
        if not isinstance(order, dict) or self._numeric_id(order.get('id')) != (
                self.shopify_order_id):
            raise ValidationError(_(
                'Shopify returned no matching canonical order.'))
        return order

    def _resolve_warehouse(self, order_payload):
        self.ensure_one()
        explicit = self._location_from_payload(order_payload)
        if explicit:
            location = self._mapped_location(explicit)
            if not location or not location.warehouse_id:
                raise ValidationError(_(
                    'Shopify location %s is not mapped to an Odoo warehouse.')
                                      % explicit)
            return location.warehouse_id, 'payload', (
                location.shopify_location_id or location.name)
        location = self._fulfillment_location()
        if location and location.warehouse_id:
            return location.warehouse_id, 'fulfillment_order', (
                location.shopify_location_id or location.name)
        if not self.instance_id.warehouse_id:
            raise ValidationError(_(
                'The Shopify instance has no default Odoo warehouse.'))
        return self.instance_id.warehouse_id, 'default', ''

    def _matching_product(self, line):
        self.ensure_one()
        company_id = self.instance_id.company_id.id
        sku = str(line.get('sku') or '').strip()
        variant_id = self._numeric_id(line.get('variant_id'))
        candidates = self.env['product.product'].sudo().browse()
        if sku:
            candidates |= self.env['product.product'].sudo().search([
                '|', ('barcode', '=', sku),
                ('shopify_variant_sku', '=', sku),
                ('shopify_sync_ids.instance_id', '=', self.instance_id.id),
                ('company_id', 'in', [company_id, False]),
            ])
        if variant_id:
            candidates |= self.env['product.product'].sudo().search([
                ('shopify_variant', '=', variant_id),
                ('company_id', 'in', [company_id, False]),
            ])
            candidates |= self.env['shopify.sync'].sudo().search([
                ('instance_id', '=', self.instance_id.id),
                ('shopify_variant_id', '=', variant_id),
                ('product_prod_id', '!=', False),
            ]).mapped('product_prod_id')
        candidates = candidates.filtered('active')
        if variant_id:
            exact = candidates.filtered(
                lambda product: str(product.shopify_variant or '') == variant_id)
            if exact:
                return exact[:1]
            sync_product_ids = set(self.env['shopify.sync'].sudo().search([
                ('instance_id', '=', self.instance_id.id),
                ('shopify_variant_id', '=', variant_id),
                ('product_prod_id', 'in', candidates.ids),
            ]).mapped('product_prod_id').ids)
            exact = candidates.filtered(lambda p: p.id in sync_product_ids)
            if exact:
                return exact[:1]
        return candidates[:1]

    def _normalise_payload(self, order_payload, location_code):
        self.ensure_one()
        order = dict(order_payload)
        order['id'] = self.shopify_order_id
        order['name'] = order.get('name') or '#%s' % self.shopify_order_id
        order['number'] = order.get('number') or ''.join(
            re.findall(r'\d+', str(order['name']))) or self.shopify_order_id
        order['created_at'] = order.get('created_at') or datetime.now(
            timezone.utc).isoformat()
        order['currency'] = order.get('currency') or 'EGP'
        order['financial_status'] = str(
            order.get('financial_status') or 'pending').lower()
        order['fulfillment_status'] = order.get('fulfillment_status')
        order['note_attributes'] = order.get('note_attributes') or []
        order['payment_gateway_names'] = (
            order.get('payment_gateway_names') or [])
        order['discount_applications'] = (
            order.get('discount_applications') or [])
        order['discount_codes'] = order.get('discount_codes') or []
        order['refunds'] = order.get('refunds') or []
        order['current_total_discounts'] = str(
            order.get('current_total_discounts') or '0')
        order['current_total_discounts_set'] = (
            order.get('current_total_discounts_set') or
            {'shop_money': {'amount': '0'}})

        customer = dict(order.get('customer') or {})
        customer['id'] = self._numeric_id(customer.get('id')) or (
            'guest-%s' % self.shopify_order_id)
        customer.setdefault('first_name', '')
        customer.setdefault('last_name', '')
        customer.setdefault('email', '')
        customer.setdefault('phone', '')
        if not (customer['first_name'] or customer['last_name']):
            customer['first_name'] = (
                customer.get('email') or customer.get('phone') or
                'Shopify Customer')
        order['customer'] = customer

        def address(value):
            result = dict(value or {})
            for key in ('first_name', 'last_name', 'address1', 'address2',
                        'city', 'province', 'country', 'zip', 'phone'):
                result.setdefault(key, '')
            return result

        order['shipping_address'] = address(order.get('shipping_address'))
        order['billing_address'] = address(
            order.get('billing_address') or order.get('shipping_address'))

        shipping_lines = [dict(item) for item in
                          (order.get('shipping_lines') or [])]
        if not shipping_lines:
            shipping_lines = [{'title': '', 'price': '0'}]
        shipping_lines[0]['code'] = location_code or (
            shipping_lines[0].get('code') or '')
        shipping_lines[0].setdefault('title', '')
        shipping_lines[0].setdefault('price', '0')
        order['shipping_lines'] = shipping_lines

        order['tax_lines'] = order.get('tax_lines') or []
        normalised_lines = []
        for position, source in enumerate(order.get('line_items') or [], 1):
            line = dict(source)
            line['id'] = self._numeric_id(line.get('id')) or (
                '%s-%s' % (self.shopify_order_id, position))
            line['variant_id'] = self._numeric_id(line.get('variant_id'))
            line['sku'] = str(line.get('sku') or '').strip()
            try:
                quantity = float(line.get('quantity') or 0)
            except (TypeError, ValueError):
                raise ValidationError(_('Line %s has an invalid quantity.') %
                                      position)
            if quantity <= 0:
                raise ValidationError(_('Line %s has no positive quantity.') %
                                      position)
            line['quantity'] = quantity
            product = self._matching_product(line)
            if not product:
                raise ValidationError(_(
                    'No Odoo product mapping for line %s (SKU %s, variant %s).'
                ) % (position, line['sku'] or '-',
                     line['variant_id'] or '-'))
            line['sku'] = line['sku'] or (
                product.shopify_variant_sku or product.barcode or '')
            if not line['sku']:
                raise ValidationError(_(
                    'Mapped product %s has no SKU/barcode.') %
                                      product.display_name)
            line['product_id'] = self._numeric_id(line.get('product_id')) or ''
            line['title'] = line.get('title') or product.display_name
            line['price'] = str(line.get('price') or '0')
            line['discount_allocations'] = (
                line.get('discount_allocations') or [])
            line['taxable'] = bool(line.get('taxable', True))
            line['tax_lines'] = line.get('tax_lines') or []
            normalised_lines.append(line)
        if not normalised_lines:
            raise ValidationError(_('The Shopify order contains no lines.'))
        order['line_items'] = normalised_lines
        return order

    def _ensure_customer_mapping(self, order_payload):
        """Create the instance-scoped customer mapping from the order body.

        This keeps the endpoint independent of a second Shopify customer API
        request and also supports guest checkouts.
        """
        self.ensure_one()
        customer = order_payload['customer']
        customer_id = str(customer['id'])
        sync = self.env['shopify.sync'].sudo().search([
            ('instance_id', '=', self.instance_id.id),
            ('shopify_customer_ref', '=', customer_id),
            ('customer_id', '!=', False),
        ], limit=1)
        if sync:
            return sync.customer_id

        email = str(customer.get('email') or '').strip()
        partner = self.env['res.partner'].sudo().browse()
        if email:
            matches = self.env['res.partner'].sudo().search([
                ('email', '=ilike', email),
                ('company_id', 'in', [self.instance_id.company_id.id, False]),
            ], limit=2)
            if len(matches) == 1:
                partner = matches
        if not partner:
            name = ' '.join(filter(None, [
                str(customer.get('first_name') or '').strip(),
                str(customer.get('last_name') or '').strip(),
            ])).strip() or email or str(customer.get('phone') or '').strip()
            partner = self.env['res.partner'].sudo().create({
                'name': name or 'Shopify Customer',
                'email': email or False,
                'phone': customer.get('phone') or False,
                'company_id': self.instance_id.company_id.id,
                'shopify_customer_ref': customer_id,
                'shopify_instance_id': self.instance_id.id,
                'synced_customer': True,
            })
        self.env['shopify.sync'].sudo().create({
            'instance_id': self.instance_id.id,
            'shopify_customer_ref': customer_id,
            'customer_id': partner.id,
        })
        return partner

    def _ensure_confirmed_and_reserved(self, order):
        self.ensure_one()
        if order.state in ('draft', 'sent') and order.order_line:
            order.with_context(skip_shopify_write=True).action_confirm()
        pickings = order.picking_ids.filtered(
            lambda picking: picking.state not in ('done', 'cancel'))
        if pickings:
            pickings.action_assign()
        stock_lines = order.order_line.filtered(
            lambda line: line.product_id.type != 'service')
        if not stock_lines:
            return 'not_applicable'
        if order.state not in ('sale', 'done'):
            return 'waiting'
        if not pickings:
            return 'waiting'
        states = set(pickings.mapped('state'))
        if states <= {'assigned'}:
            return 'reserved'
        if 'partially_available' in states:
            return 'partial'
        return 'waiting'

    def _queue_inventory_push(self, order):
        self.ensure_one()
        warehouse = order.warehouse_id or self.warehouse_id
        products = order.order_line.mapped('product_id').filtered(
            lambda product: product.type != 'service')
        if not warehouse or not products:
            return
        locations = self.env['shopify.location'].sudo().search([
            ('instance_id', '=', self.instance_id.id),
            ('warehouse_id', '=', warehouse.id),
            ('active', '=', True),
            ('shopify_location_id', '!=', False),
        ])
        if not locations:
            raise ValidationError(_(
                'Warehouse %s is not mapped to an active Shopify location.') %
                                  warehouse.display_name)
        groups = self.env['sync.inventory'].sudo()._build_inventory_groups(
            self.instance_id, products=products)
        if not groups:
            raise ValidationError(_(
                'The ordered products have no Shopify inventory mapping.'))
        model = self.env['ir.model'].sudo().search(
            [('model', '=', 'sync.inventory')], limit=1)
        for offset in range(0, len(groups), INVENTORY_BATCH_SIZE):
            self.env['job.cron'].sudo().create({
                'model_id': model.id,
                'function': 'export_inventory_to_shopify',
                'data': {
                    'groups': groups[offset:offset + INVENTORY_BATCH_SIZE],
                    'warehouse_ids': [warehouse.id],
                    'force': True,
                },
                'instance_id': self.instance_id.id,
            })

    def _process(self):
        self.ensure_one()
        self.write({
            'state': 'processing',
            'attempts': self.attempts + 1,
            'last_attempt_at': fields.Datetime.now(),
            'error_message': False,
        })
        existing_order = self._find_order()
        try:
            if existing_order:
                with self.env.cr.savepoint():
                    reservation = self._ensure_confirmed_and_reserved(
                        existing_order)
                    self._queue_inventory_push(existing_order)
                self.write({
                    'state': ('duplicate' if reservation in
                              ('reserved', 'not_applicable') else 'blocked'),
                    'order_id': existing_order.id,
                    'warehouse_id': existing_order.warehouse_id.id,
                    'reservation_state': reservation,
                    'processed_at': fields.Datetime.now(),
                    'error_message': False,
                })
                return

            # The Flow payload identifies the order and authenticates the
            # event. Values used to build the sale order are fetched from
            # Shopify through the connector's own token.
            source_payload = self._fetch_canonical_order()
            with self.env.cr.savepoint():
                warehouse, source, location_code = self._resolve_warehouse(
                    source_payload)
                order_payload = self._normalise_payload(
                    source_payload, location_code)
                self._ensure_customer_mapping(order_payload)
                wizard = self.env['sale.order.sync'].sudo().create({
                    'import_orders': 'odoo',
                    'shopify_instance_id': self.instance_id.id,
                    'type_order': 'confirmed',
                })
                wizard.import_confirmed_orders_from_shopify(
                    [order_payload], self.instance_id, wizard.id)
                order = self._find_order()
                if not order or not order.order_line:
                    raise ValidationError(_(
                        'The existing Shopify importer did not create a '
                        'complete Odoo order.'))
                reservation = self._ensure_confirmed_and_reserved(order)
                self._queue_inventory_push(order)
            self.write({
                'state': ('done' if reservation in
                          ('reserved', 'not_applicable') else 'blocked'),
                'order_id': order.id,
                'warehouse_id': warehouse.id,
                'warehouse_source': source,
                'reservation_state': reservation,
                'processed_at': fields.Datetime.now(),
                'error_message': (
                    False if reservation in ('reserved', 'not_applicable')
                    else 'Order imported but stock is not fully reserved.'),
            })
        except ValidationError as error:
            self.write({
                'state': 'blocked',
                'order_id': existing_order.id if existing_order else False,
                'warehouse_id': (
                    existing_order.warehouse_id.id if existing_order else
                    False),
                'error_message': str(error)[:4000],
                'processed_at': fields.Datetime.now(),
            })
            _logger.warning('Shopify order event %s blocked: %s',
                            self.event_id, error)
        except Exception as error:
            self.write({
                'state': 'failed',
                'order_id': existing_order.id if existing_order else False,
                'error_message': '%s: %s' % (
                    type(error).__name__, str(error)[:3900]),
                'processed_at': fields.Datetime.now(),
            })
            _logger.exception('Shopify order event %s failed', self.event_id)

    @api.model
    def _cron_retry_order_events(self):
        cutoff = fields.Datetime.subtract(fields.Datetime.now(), minutes=1)
        events = self.sudo().search([
            ('state', 'in', ('received', 'blocked', 'failed')),
            ('attempts', '<', 1440),
            '|', ('last_attempt_at', '=', False),
            ('last_attempt_at', '<=', cutoff),
        ], order='received_at asc', limit=50)
        for event in events:
            try:
                with self.env.cr.savepoint():
                    event._process()
            except Exception:
                _logger.exception(
                    'Uncaught retry failure for Shopify event %s',
                    event.event_id)

    def action_retry(self):
        for event in self:
            event.sudo()._process()
        return True

    @api.model
    def _cron_redact_old_payloads(self):
        cutoff = fields.Datetime.subtract(fields.Datetime.now(), days=30)
        self.sudo().search([
            ('received_at', '<', cutoff),
            ('payload', '!=', False),
            ('state', 'in', ('done', 'duplicate')),
        ], limit=1000).write({'payload': False})

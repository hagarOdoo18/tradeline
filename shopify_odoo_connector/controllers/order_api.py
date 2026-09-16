# -*- coding: utf-8 -*-
################################################################################
#
#    Tradeline - Shopify confirmed order intake API
#
#    A small, explicit REST endpoint that a Shopify developer can POST a
#    *confirmed* order to. Unlike the webhook routes in ``webhook.py`` it is
#    authenticated with a Bearer token (POST /api/shopify/v1/auth with the
#    instance's Store Name + Client Secret), it is idempotent, it answers with real
#    HTTP status codes and a JSON body describing what was created in Odoo, and
#    it confirms the sale order (so the delivery is generated) and invoices it.
#
################################################################################
import hmac
import json
import logging
import pprint

from datetime import datetime

from odoo import SUPERUSER_ID
from odoo import fields, http
from odoo.http import request
from odoo.tools import html2plaintext


_logger = logging.getLogger(__name__)

# Endpoint version, echoed back so the caller can detect contract changes.
API_VERSION = '1.0'

AUTH_PATH = '/api/shopify/v1/auth'

# Default behaviour once the order is accepted. Each one can be turned off
# per request through the optional "odoo_options" object in the payload.
DEFAULT_OPTIONS = {
    'confirm': True,        # call action_confirm() -> delivery is created
    'invoice': False,        # create and post the customer invoice
    'register_payment': False,  # only ever done when Shopify says it is paid
}


class _OrderRejected(Exception):
    """Raised inside an order's savepoint to roll that order back and
    answer with an error for it."""

    def __init__(self, code, message, status, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra


class ShopifyOrderApi(http.Controller):
    """Inbound REST API for confirmed Shopify orders.

    Routes:
        POST /api/shopify/v1/auth
            Store Name + Client Secret -> Bearer token valid 24 hours.
        GET  /api/shopify/v1/ping
            Token / connectivity check for the Shopify developer.
        POST /api/shopify/v1/orders/confirmed
            Create (or return the already created) Odoo sale order for one
            Shopify order, and confirm it.
    """


    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _model(model, company=None):
        """Return ``model`` bound to the superuser (and optionally a company).

        The route runs with ``auth='none'`` so there is no user on the
        request; every ORM access has to be elevated explicitly.
        """
        records = request.env[model].with_user(SUPERUSER_ID).sudo()
        if company:
            records = records.with_company(company)
        return records

    @staticmethod
    def _respond(payload, status=200):
        """Return a real JSON HTTP response (not the JSON-RPC envelope)."""
        payload = dict(payload, api_version=API_VERSION)
        return request.make_json_response(payload, status=status)

    @staticmethod
    def _error(code, message, status, **extra):
        body = {'success': False, 'error': code, 'message': message}
        body.update(extra)
        return ShopifyOrderApi._respond(body, status=status)

    @staticmethod
    def _bearer_token():
        """The token from ``Authorization: Bearer <token>``, or ''."""
        header = request.httprequest.headers.get('Authorization') or ''
        if header[:7].lower() == 'bearer ':
            return header[7:].strip()
        return ''

    @staticmethod
    def _same_shop(instance, shop_domain):
        shop_name = (instance.shop_name or '').lower()
        shop_domain = (shop_domain or '').lower()
        return shop_domain == shop_name or shop_domain in shop_name

    @staticmethod
    def _secrets_match(stored, supplied):
        """Constant-time comparison. An empty secret never matches."""
        if not stored or not supplied:
            return False
        try:
            return hmac.compare_digest(str(stored).encode('utf-8'),
                                       str(supplied).encode('utf-8'))
        except (TypeError, ValueError):
            return False

    def _authenticate(self, data=None):
        """Resolve the Shopify instance from the Bearer token.

        The token comes from ``POST /api/shopify/v1/auth``. When the caller
        also sends ``X-Shopify-Shop-Domain`` it must be the token's shop.

            tuple: ``(instance, error_response)`` - exactly one is set.
        """
        token = self._bearer_token()
        if not token:
            return None, self._error(
                'missing_token',
                'Send "Authorization: Bearer <token>". Get a token from '
                'POST %s.' % AUTH_PATH, 401)

        record = self._model('shopify.api.token')._find(token)
        if not record:
            return None, self._error('invalid_token', 'Invalid token.', 401)
        if record.expires_at <= fields.Datetime.now():
            return None, self._error(
                'token_expired',
                'The token expired at %sZ. Get a new one from POST %s.' % (
                    record.expires_at.isoformat(), AUTH_PATH), 401)

        instance = record.instance_id
        shop_domain = (request.httprequest.headers.get('X-Shopify-Shop-Domain')
                       or (data or {}).get('shop_domain') or '')
        if shop_domain and not self._same_shop(instance, shop_domain):
            return None, self._error(
                'shop_mismatch',
                'This token belongs to shop "%s", not "%s".' % (
                    instance.shop_name, shop_domain), 403)

        record.write({'last_used': fields.Datetime.now()})
        return instance, None

    def _log(self, instance, message):
        """Write a log.message record, best effort."""
        try:
            self._model('log.message').create({
                'name': message,
                'shopify_instance_id': instance.id if instance else False,
                'model': 'Sale Order',
            })
        except Exception:  # pragma: no cover - logging must never raise
            _logger.exception('Could not write shopify log.message')

    # ------------------------------------------------------------------
    # mapping helpers
    # ------------------------------------------------------------------
    # Customer, addresses, warehouse/branch/team/salesperson/journal, tax,
    # product selection, Shopify prices and confirmation are NOT mapped here:
    # the order is built by sale.order.sync.import_confirmed_orders_from_shopify
    # -- the very cycle the scheduled confirmed-order import runs -- so an
    # order pushed through this API and the same order pulled by the cron end
    # up identical in Odoo.
    @staticmethod
    def _lines_report(data, lines):
        """Per Shopify line: which Odoo product it landed on, or skipped."""
        by_ref = {}
        for line in lines:
            by_ref.setdefault(str(line.shopify_line_ref), line)
        report = []
        for item in data.get('line_items') or []:
            line = by_ref.get(str(item.get('id')))
            report.append({
                'shopify_line_id': item.get('id'),
                'sku': item.get('sku'),
                'variant_id': item.get('variant_id'),
                'odoo_line_id': line.id if line else None,
                'odoo_product_id': line.product_id.id if line else None,
                'odoo_product': line.product_id.display_name if line else None,
                'skipped': not line,
            })
        return report

    def _is_paid(self, data, instance):
        """True when Shopify considers the order paid."""
        if (data.get('financial_status') or '').lower() == 'paid':
            return True
        payment = self._model('shopify.payment').search(
            [('shopify_order_ref', '=', str(data.get('id'))),
             ('shopify_instance_id', '=', instance.id)], limit=1)
        return payment.payment_status == 'paid'

    # ------------------------------------------------------------------
    # routes
    # ------------------------------------------------------------------
    @http.route(AUTH_PATH, type='http', auth='none', methods=['POST'],
                csrf=False, save_session=False)
    def authenticate(self, **kwargs):
        """Exchange the instance's Store Name + Client Secret for a token.

        Body (JSON)::

            {"store_name": "tradelinestores-2.myshopify.com",
             "client_secret": "<Client Secret of the Shopify instance>"}

        ``store_name`` is the Store Name of the Shopify instance in Odoo
        (``shop_domain`` is accepted as an alias). ``client_secret`` is the
        instance's Client Secret, compared in constant time.

            200 - ``access_token`` (valid ``expires_in`` seconds);
            400 / 401 / 500 - see the ``error`` code.
        """
        try:
            try:
                body = json.loads(
                    request.httprequest.get_data(as_text=True) or '{}')
            except ValueError:
                return self._error('invalid_json',
                                   'Request body is not valid JSON.', 400)
            if not isinstance(body, dict):
                return self._error('invalid_json',
                                   'Request body must be a JSON object.', 400)

            store_name = (body.get('store_name') or body.get('shop_domain')
                          or '').strip()
            client_secret = body.get('client_secret') or ''
            if not store_name or not client_secret:
                return self._error(
                    'missing_credentials',
                    'Send "store_name" and "client_secret".', 400)

            instance = self._model('shopify.configuration').search(
                [('shop_name', '=ilike', store_name)], limit=1)
            # Same answer for an unknown store and a wrong secret, so the
            # endpoint cannot be used to discover which stores exist.
            if not instance or not self._secrets_match(
                    instance.consumer_secret, client_secret):
                _logger.warning('Shopify order API: refused token request '
                                'for store %r', store_name)
                return self._error('invalid_credentials',
                                   'Wrong store name or client secret.', 401)

            token, record, created = self._model(
                'shopify.api.token')._issue(instance)
            _logger.info('Shopify order API: %s token for %s',
                         'issued new' if created else 'returned live',
                         instance.name)
            expires_in = int(
                (record.expires_at - fields.Datetime.now()).total_seconds())
            return self._respond({
                'success': True,
                'access_token': token,
                'token_type': 'Bearer',
                'expires_in': max(expires_in, 0),
                'reused': not created,
                'expires_at': record.expires_at.isoformat() + 'Z',
                'instance': instance.name,
                'store_name': instance.shop_name,
                'company': instance.company_id.display_name,
            })
        except Exception as error:  # noqa: BLE001 - the endpoint must answer
            _logger.exception('Shopify order API: authentication failed')
            try:
                request.env.cr.rollback()
            except Exception:  # pragma: no cover
                pass
            return self._error('server_error',
                               'Authentication could not be processed: %s'
                               % error, 500)

    @http.route('/api/shopify/v1/ping', type='http', auth='none',
                methods=['GET'], csrf=False, save_session=False)
    def ping(self, **kwargs):
        """Token check. Returns the instance the token belongs to."""
        instance, error = self._authenticate()
        if error:
            return error
        return self._respond({
            'success': True,
            'instance': instance.name,
            'shop_domain': instance.shop_name,
            'company': instance.company_id.display_name,
            'warehouse': instance.warehouse_id.display_name or None,
            'server_time': datetime.utcnow().isoformat() + 'Z',
        })

    @http.route('/api/shopify/v1/orders/confirmed', type='http', auth='none',
                methods=['POST'], csrf=False, save_session=False)
    def create_confirmed_order(self, **kwargs):
        """Create (and confirm) the Odoo sale order for one Shopify order.

        Body: the native Shopify Order JSON (the same object the
        ``orders/create`` webhook sends), optionally wrapped in an ``order``
        key, plus an optional ``odoo_options`` object.

            201 - the sale order was created;
            200 - the Shopify order was already imported (nothing changed);
            400 / 401 / 403 / 404 / 422 / 500 - see the ``error`` code.
        """
        instance = None
        try:
            raw = request.httprequest.get_data(as_text=True)
            try:
                body = json.loads(raw or '{}')
            except ValueError:
                return self._error('invalid_json',
                                   'Request body is not valid JSON.', 400)
            if not isinstance(body, dict):
                return self._error('invalid_json',
                                   'Request body must be a JSON object.', 400)

            data = body.get('order') if isinstance(
                body.get('order'), dict) else body
            options = body.get('odoo_options') if isinstance(
                body.get('odoo_options'), dict) else {}

            instance, error = self._authenticate(data)
            if error:
                return error

            status, payload = self._process_order(data, instance, options)
            return self._respond(payload, status=status)

        except Exception as error:  # noqa: BLE001 - the endpoint must answer
            _logger.exception('Shopify confirmed-order API failed')
            try:
                request.env.cr.rollback()
            except Exception:  # pragma: no cover
                pass
            self._log(instance,
                      'Confirmed order API failed: <pre>%s</pre>'
                      % pprint.pformat(str(error)))
            return self._error(
                'server_error',
                'The order could not be processed: %s' % error, 500)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    @staticmethod
    def _order_options(data, request_options):
        options = dict(DEFAULT_OPTIONS)
        for supplied in (request_options, data.get('odoo_options')):
            if isinstance(supplied, dict):
                options.update({key: bool(value)
                                for key, value in supplied.items()
                                if key in DEFAULT_OPTIONS})
        return options

    def _process_order(self, data, instance, request_options):
        """Import one Shopify order. Never raises.

        Everything the order writes happens inside a savepoint; a rejected
        or failed order is rolled back to it, while the log line explaining
        why is still written.

            tuple: ``(http_status, payload)``.
        """
        if not isinstance(data, dict):
            return 400, self._error_body(
                'invalid_order', 'Each order must be a JSON object.')

        shopify_order_id = data.get('id')
        if not shopify_order_id:
            return 400, self._error_body(
                'missing_order_id', 'The payload has no Shopify order "id".')
        shopify_order_id = str(shopify_order_id)

        if not (data.get('line_items') or []):
            return 400, self._error_body(
                'empty_order', 'The payload has no "line_items".',
                shopify_order_id=shopify_order_id)

        options = self._order_options(data, request_options)
        try:
            with request.env.cr.savepoint():
                return self._import_order(
                    data, instance, shopify_order_id, options)
        except _OrderRejected as rejected:
            # the savepoint is rolled back; log outside of it
            request.env.invalidate_all()
            self._log(instance, 'Shopify order %s not imported: %s' % (
                shopify_order_id, rejected.message))
            return rejected.status, self._error_body(
                rejected.code, rejected.message,
                shopify_order_id=shopify_order_id, **rejected.extra)
        except Exception as error:  # noqa: BLE001
            request.env.invalidate_all()
            _logger.exception('Shopify confirmed-order API: order %s failed',
                              shopify_order_id)
            self._log(instance,
                      'Confirmed order API failed for order %s: <pre>%s</pre>'
                      % (shopify_order_id, pprint.pformat(str(error))))
            return 500, self._error_body(
                'server_error',
                'The order could not be processed: %s' % error,
                shopify_order_id=shopify_order_id)

    def _import_order(self, data, instance, shopify_order_id, options):
        """Body of `_process_order`, run inside the order's savepoint.
        Raises `_OrderRejected` to roll the order back."""
        # -- idempotency ----------------------------------------------------
        existing = self._model('shopify.sync').search([
            ('instance_id', '=', instance.id),
            ('shopify_order_ref', '=', shopify_order_id),
            ('order_id', '!=', False),
        ], limit=1)
        if existing.order_id:
            order = existing.order_id
            return 200, {
                'success': True,
                'duplicate': True,
                'message': 'This Shopify order was already imported.',
                'shopify_order_id': shopify_order_id,
                'sale_order_id': order.id,
                'sale_order': order.name,
                'state': order.state,
                'invoice_ids': order.invoice_ids.ids,
            }

        company = instance.company_id
        warnings = []


        # -- creation: the same cycle as the scheduled import ----------------
        # The page importer is called with a one-order page. Its wizard
        # record carries the "confirm" option (draft=True -> not confirmed).
        # skip_shopify_write: the order came FROM Shopify, so the sale.order
        # writes/confirmation must not push it back.
        # Remember where the log stood, so a failure is explained only by
        # order-import lines written during THIS import - never by the
        # inventory cron's lines that share the same table.
        log_model = self._model('log.message')
        last_log = log_model.search([], order='id desc', limit=1)
        sync_model = self._model('sale.order.sync', company).with_context(
            skip_shopify_write=True)
        sync_wizard = sync_model.create({
            'shopify_instance_id': instance.id,
            'import_orders': 'odoo',
            'type_order': 'confirmed',
            'draft': not options['confirm'],
        })
        sync_model.import_confirmed_orders_from_shopify(
            [data], instance, sync_wizard.id)

        order = self._model('shopify.sync').search([
            ('instance_id', '=', instance.id),
            ('shopify_order_ref', '=', shopify_order_id),
            ('order_id', '!=', False),
        ], limit=1).order_id
        if not order:
            # the importer logs and swallows its failures: surface the
            # order log line it wrote during this import as the reason
            reason = log_model.search([
                ('id', '>', last_log.id or 0),
                ('shopify_instance_id', '=', instance.id),
                ('model', '=', 'sale.order'),
            ], order='id desc', limit=1).name
            # log.message.name is HTML; the API caller gets plain text
            reason = html2plaintext(reason).strip() if reason else ''
            raise _OrderRejected(
                'not_imported',
                'The order was not imported: %s' % (
                    reason or 'the importer skipped it without logging a '
                              'reason (see the Shopify log)'), 422)

        if not order.order_line:
            # The import keeps a header without lines; an API caller is
            # better served by nothing at all, so a retry after fixing the
            # products is not answered with "duplicate".
            raise _OrderRejected(
                'no_order_lines',
                'No order line could be created - none of the line items '
                'matched an Odoo product (see the Shopify log).', 422)

        confirmed = order.state == 'sale'
        lines_report = self._lines_report(data, order.order_line)
        skipped = [line for line in lines_report if line['skipped']]
        if skipped:
            warnings.append(
                '%s line item(s) skipped - product not found: %s' % (
                    len(skipped), ', '.join(
                        str(line['sku'] or line['shopify_line_id'])
                        for line in skipped)))
        if options['confirm'] and not confirmed:
            warnings.append(
                'The order was created but could not be confirmed; it stays '
                'in draft (see the Shopify log for the reason).')

        # if options['invoice'] and order.state == 'sale':
        #     try:
        #         invoices = order._create_invoices()
        #         if invoices:
        #             invoices.action_post()
        #             if options['register_payment'] and self._is_paid(
        #                     data, instance):
        #                 self._register_payment(invoices, warnings)
        #     except Exception as error:
        #         _logger.exception('Shopify order %s: invoicing failed',
        #                           shopify_order_id)
        #         warnings.append('Invoicing failed: %s' % error)

        response = {
            'success': True,
            'duplicate': False,
            'shopify_order_id': shopify_order_id,
            'shopify_order_name': data.get('name'),
            'sale_order_id': order.id,
            'sale_order': order.name,
            'state': order.state,
            'company': company.display_name,
            'warehouse': order.warehouse_id.display_name or None,
            'partner_id': order.partner_id.id,
            'amount_total': order.amount_total,
            'picking_ids': order.picking_ids.ids,
            'confirmed': confirmed,
            'lines': lines_report,
        }
        if warnings:
            response['warnings'] = warnings
        return 201, response

    @staticmethod
    def _error_body(code, message, **extra):
        body = {'success': False, 'error': code, 'message': message}
        body.update(extra)
        return body

    def _register_payment(self, invoices, warnings):
        """Register a full payment on the freshly posted invoice(s)."""
        try:
            wizard = self._model(
                'account.payment.register',
                invoices.company_id[:1]).with_context(
                active_model='account.move',
                active_ids=invoices.ids).create({})
            wizard._create_payments()
        except Exception as error:
            _logger.exception('Payment registration failed for %s',
                              invoices.ids)
            warnings.append('Payment registration failed: %s' % error)

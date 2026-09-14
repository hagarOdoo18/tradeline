# -*- coding: utf-8 -*-
"""Authenticated Shopify -> Odoo order ingress."""

import hmac
import json
import logging

from odoo import http
from odoo.http import request


_logger = logging.getLogger(__name__)
MAX_BODY_BYTES = 2 * 1024 * 1024


class ShopifyOrderAPI(http.Controller):

    @staticmethod
    def _json_response(payload, status=200):
        return request.make_json_response(payload, status=status)

    @http.route('/api/shopify/v1/orders/confirmed', type='http', auth='none',
                methods=['POST'], csrf=False, save_session=False)
    def confirmed_order(self, **_kwargs):
        """Accept one Shopify order and reserve it in Odoo exactly once."""
        http_request = request.httprequest
        content_length = http_request.content_length
        if content_length is not None and content_length > MAX_BODY_BYTES:
            return self._json_response(
                {'status': 'rejected', 'error': 'request_too_large'}, 413)

        raw = http_request.get_data(cache=False)
        if len(raw) > MAX_BODY_BYTES:
            return self._json_response(
                {'status': 'rejected', 'error': 'request_too_large'}, 413)
        try:
            payload = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._json_response(
                {'status': 'rejected', 'error': 'invalid_json'}, 400)
        if not isinstance(payload, dict):
            return self._json_response(
                {'status': 'rejected', 'error': 'json_object_required'}, 400)

        shop_domain = http_request.headers.get('X-Shopify-Shop-Domain', '')
        api_key = http_request.headers.get('X-Odoo-Api-Key', '')
        instance = request.env['shopify.order.event'].sudo().find_instance(
            shop_domain)
        # Do not reveal whether the shop or the key was wrong.
        expected = instance.order_api_key if instance else ''
        if not (instance and api_key and expected and
                hmac.compare_digest(str(api_key), str(expected))):
            _logger.warning('Rejected Shopify order request for domain %s',
                            shop_domain or '(missing)')
            return self._json_response(
                {'status': 'rejected', 'error': 'unauthorized'}, 401)

        topic = http_request.headers.get(
            'X-Odoo-Event-Type', 'order.created').strip().lower()
        supplied_event_id = http_request.headers.get('X-Odoo-Event-Id', '')
        try:
            event, created = request.env[
                'shopify.order.event'].sudo().accept_and_process(
                    instance, payload, topic=topic,
                    supplied_event_id=supplied_event_id)
        except ValueError as error:
            return self._json_response(
                {'status': 'rejected', 'error': str(error)}, 400)
        except Exception:
            _logger.exception(
                'Unexpected failure accepting a Shopify order for %s',
                instance.display_name)
            return self._json_response(
                {'status': 'failed', 'error': 'internal_error'}, 503)

        response = {
            'status': event.state,
            'event_id': event.event_id,
            'created': created,
            'order': event.shopify_order_name or event.shopify_order_id,
            'odoo_order_id': event.order_id.id or None,
            'reservation': event.reservation_state or None,
        }
        if event.state == 'failed':
            response['error'] = 'processing_failed'
            return self._json_response(response, 503)
        # A duplicate is already complete. New and blocked events use 202:
        # Odoo has accepted the event and owns any subsequent retry.
        return self._json_response(
            response, 200 if (not created or event.state == 'duplicate')
            else 202)

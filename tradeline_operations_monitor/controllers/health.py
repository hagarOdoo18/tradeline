from odoo import http
from odoo.http import request


class OperationsHealth(http.Controller):
    @http.route('/tradeline/operations/health', type='http', auth='public', methods=['GET'],
                csrf=False, save_session=False, readonly=True)
    def health(self, **kwargs):
        authorization = request.httprequest.headers.get('Authorization', '')
        token = authorization.removeprefix('Bearer ') if authorization.startswith('Bearer ') else ''
        monitor = request.env['tradeline.operations.monitor'].sudo().with_context(active_test=False).search([], limit=1)
        payload, status = monitor._health_payload(token) if monitor else ({'status': 'unavailable'}, 503)
        return request.make_json_response(payload, status=status,
                                          headers=[('Cache-Control', 'no-store'), ('X-Robots-Tag', 'noindex')])

# -*- coding: utf-8 -*-
from odoo import http
from odoo.http import request


class PosSerialValidationController(http.Controller):

    @http.route(
        '/pos/serial/validate',
        type='json',
        auth='user',
        methods=['POST'],
    )
    def validate_serial(self, serial_name, product_id, pos_config_id=None, **kwargs):
        """
        Endpoint للتحقق من الـ Serial من الـ POS Frontend.

        Request body:
            {
                "serial_name": "SN-001",
                "product_id": 42,
                "pos_config_id": 1   // اختياري
            }

        Response:
            {
                "valid": true/false,
                "message": "...",
                "lot_id": 15 or false
            }
        """
        if not serial_name or not product_id:
            return {
                'valid': False,
                'message': 'يرجى تحديد الرقم التسلسلي والمنتج.',
                'lot_id': False,
            }

        result = request.env['stock.lot'].validate_serial_for_pos(
            serial_name=serial_name,
            product_id=int(product_id),
            pos_config_id=int(pos_config_id) if pos_config_id else None,
        )
        return result

    @http.route(
        '/pos/serial/search',
        type='json',
        auth='user',
        methods=['POST'],
    )
    def search_serials(self, product_id, pos_config_id=None, query='', **kwargs):
        """
        البحث عن الأرقام التسلسلية المتاحة لمنتج معين.
        مفيد لـ Autocomplete في الـ POS.
        """
        domain = [
            ('product_id', '=', int(product_id)),
            ('company_id', '=', request.env.company.id),
            ('is_sold_in_pos', '=', False),
        ]
        if query:
            domain.append(('name', 'ilike', query))

        lots = request.env['stock.lot'].search(domain, limit=20)

        # فلترة حسب الكمية المتاحة
        available_lots = []
        for lot in lots:
            qty = lot._get_available_qty_in_pos_location(
                int(pos_config_id) if pos_config_id else None
            )
            if qty > 0:
                available_lots.append({
                    'id': lot.id,
                    'name': lot.name,
                    'qty': qty,
                })

        return {'lots': available_lots}

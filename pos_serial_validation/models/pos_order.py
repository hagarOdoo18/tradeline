# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError


class PosOrder(models.Model):
    _inherit = 'pos.order'

    # ============================================================
    #  Override: التحقق عند تأكيد الأوردر
    # ============================================================

    def _process_order(self, order,  existing_order):
        """Override للتحقق من الأرقام التسلسلية قبل معالجة الأوردر"""
        self._validate_order_serials(order)
        return super()._process_order(order, existing_order)

    @api.model
    def _get_pos_config_from_order(self, order_vals):
        """
        استخراج pos.config من order_vals عبر session_id.
        يُستخدم لجلب الـ Source Location الخاص بنقطة البيع.

        :return: pos.config record أو False
        """
        session_id = order_vals.get('session_id')
        if not session_id:
            return False
        session = self.env['pos.session'].browse(session_id)
        return session.config_id if session.exists() else False

    @api.model
    def _validate_order_serials(self, order_vals):
        """
        التحقق من جميع الأرقام التسلسلية في الأوردر (بيع فقط، مش مرتجع).

        ترتيب التحققات لكل Serial:
          1. منع التكرار داخل نفس الفاتورة
          2. Serial موجود في النظام
          3. حالته مش 'sold'
          4. ✨ موجود في موقع الـ POS (source location من picking type)
        """
        lines = order_vals.get('lines', [])
        seen_serials = set()

        # ── جلب الـ POS Config وموقعه مرة واحدة لكل الأوردر ──
        pos_config  = self._get_pos_config_from_order(order_vals)
        pos_config_id = pos_config.id if pos_config else False

        # جلب الـ Source Location مباشرة لاستخدامها في التحقق
        StockLot = self.env['stock.lot']
        Stockqty = self.env['stock.quant']
        pos_location = StockLot._get_pos_source_location(pos_config_id)


        for line_cmd in lines:
            if isinstance(line_cmd, (list, tuple)) and len(line_cmd) == 3:
                line_vals = line_cmd[2] if line_cmd[2] else {}
            elif isinstance(line_cmd, dict):
                line_vals = line_cmd
            else:
                continue
            product_id   = line_vals.get('product_id')
            pack_lot_ids = line_vals.get('pack_lot_ids', [])

            if line_cmd[2]['refunded_orderline_id']:
                for lot_cmd in pack_lot_ids:
                    if isinstance(lot_cmd, (list, tuple)) and len(lot_cmd) == 3:
                        lot_vals = lot_cmd[2] if lot_cmd[2] else {}
                    elif isinstance(lot_cmd, dict):
                        lot_vals = lot_cmd
                    else:
                        continue
                    lot_name = lot_vals.get('lot_name') or lot_vals.get('lot_id')
                    if not lot_name:
                        continue

                    serial_key = (str(lot_name), product_id)
                    if serial_key in seen_serials:
                        raise UserError(_(
                            'الرقم التسلسلي "%s" مكرر داخل نفس الفاتورة.'
                        ) % lot_name)
                    seen_serials.add(serial_key)

                return True


            qty = line_vals.get('qty', 0)

            # ── سطور المرتجعات (qty سالب) تتخطى التحقق ─────────
            if qty < 0:
                continue

            if not product_id:
                continue

            product = self.env['product.product'].browse(product_id)
            if product.tracking not in ('serial', 'lot'):
                stock_qty = Stockqty.search([
                    ('product_id', '=', product_id),
                    ('location_id', '=', pos_location.id),
                ], limit=1)
                ava_qty = sum(stock_qty.mapped('quantity')) - sum(stock_qty.mapped('reserved_quantity'))
                if  ava_qty< qty:
                    raise UserError(_(
                        'الكميه في المخزن لاتغطي الطلب من المنتج "%s".'
                    ) % product.display_name)
                continue


            for lot_cmd in pack_lot_ids:
                if isinstance(lot_cmd, (list, tuple)) and len(lot_cmd) == 3:
                    lot_vals = lot_cmd[2] if lot_cmd[2] else {}
                elif isinstance(lot_cmd, dict):
                    lot_vals = lot_cmd
                else:
                    continue

                lot_name = lot_vals.get('lot_name') or lot_vals.get('lot_id')
                if not lot_name:
                    continue

                # ── 1. منع التكرار داخل نفس الأوردر ──────────
                serial_key = (str(lot_name), product_id)
                if serial_key in seen_serials:
                    raise UserError(_(
                        'الرقم التسلسلي "%s" مكرر داخل نفس الفاتورة.'
                    ) % lot_name)
                seen_serials.add(serial_key)

                # ── 2. جلب الـ Lot ─────────────────────────────
                if isinstance(lot_name, int):
                    lot = StockLot.browse(lot_name)
                    serial_name = lot.name
                else:
                    serial_name = lot_name
                    lot = StockLot.search([
                        ('name',       '=', serial_name),
                        ('product_id', '=', product_id),
                        ('location_id', '=',pos_location.id),

                    ], limit=1)

                if not lot :
                    raise UserError(_(
                        'الرقم التسلسلي "%s" غير موجود في المخزن.'
                    ) % serial_name)

                # ── 3. التحقق من الحالة ────────────────────────
                if lot.serial_status == 'sold' or lot.product_qty!=1:
                    raise UserError(_(
                        'الرقم التسلسلي "%s" مباع حالياً ولم يُرتجع بعد.\n'
                        'لا يمكن إعادة بيع رقم تسلسلي مباع.'
                    ) % serial_name)



class PosOrderLine(models.Model):
    _inherit = 'pos.order.line'

    def _is_return_line(self):
        """هل هذا السطر مرتجع؟ (qty سالب)"""
        return self.qty < 0

    def action_mark_serials_sold(self):
        """تحديد الأرقام التسلسلية كمباعة بعد الدفع"""
        for line in self.filtered(lambda l: not l._is_return_line()):
            for pack_lot in line.pack_lot_ids:
                lot = self.env['stock.lot'].search([
                    ('name',       '=', pack_lot.lot_name),
                    ('product_id', '=', line.product_id.id),
                    ('company_id', '=', self.env.company.id),
                ], limit=1)
                if lot:
                    lot.action_mark_sold(order_line=line)

    def action_mark_serials_returned(self):
        """
        تحديد الأرقام التسلسلية كمرتجعة.
        يُستدعى عند تأكيد Return Order في POS.
        """
        for line in self.filtered(lambda l: l._is_return_line()):
            for pack_lot in line.pack_lot_ids:
                lot = self.env['stock.lot'].search([
                    ('name',       '=', pack_lot.lot_name),
                    ('product_id', '=', line.product_id.id),
                    ('company_id', '=', self.env.company.id),
                ], limit=1)
                if lot and lot.serial_status == 'sold':
                    lot.action_mark_returned(order_line=line)


class PosOrderWithReturn(models.Model):
    _inherit = 'pos.order'

    def action_pos_order_paid(self):
        """تحديث حالة الـ Serials بعد الدفع (بيع أو مرتجع)"""
        res = super().action_pos_order_paid()
        for order in self:
            # ── بيع ────────────────────────────────────────────
            order.lines.action_mark_serials_sold()
            # ── مرتجع ──────────────────────────────────────────
            order.lines.action_mark_serials_returned()
        return res
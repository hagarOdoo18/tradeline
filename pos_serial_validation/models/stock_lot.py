# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import ValidationError


class StockLot(models.Model):
    _inherit = 'stock.lot'

    # ============================================================
    #  الحقل الرئيسي: حالة الـ Serial
    # ============================================================

    serial_status = fields.Selection(
        selection=[
            ('available', '✅ متاح للبيع'),
            ('sold',      '🛒 مباع'),
            ('returned',  '↩️ مرتجع'),
        ],
        string='حالة الـ Serial',
        default='available',
        required=True,
        copy=False,
        index=True,
        tracking=True,
    )

    is_sold_in_pos = fields.Boolean(
        string='مباع في POS',
        compute='_compute_is_sold_in_pos',
        store=True,
        copy=False,
    )

    pos_order_line_id = fields.Many2one(
        comodel_name='pos.order.line',
        string='آخر سطر بيع',
        readonly=True,
        copy=False,
    )

    return_count = fields.Integer(
        string='عدد المرات المرتجعة',
        default=0,
        readonly=True,
        copy=False,
    )

    serial_history_ids = fields.One2many(
        comodel_name='pos.serial.history',
        inverse_name='lot_id',
        string='سجل العمليات',
        readonly=True,
    )

    history_count = fields.Integer(
        string='عدد العمليات',
        compute='_compute_history_count',
    )

    # ============================================================
    #  Computed Fields
    # ============================================================

    @api.depends('serial_status')
    def _compute_is_sold_in_pos(self):
        for lot in self:
            lot.is_sold_in_pos = (lot.serial_status == 'sold')

    def _compute_history_count(self):
        for lot in self:
            lot.history_count = len(lot.serial_history_ids)

    # ============================================================
    #  Constraints
    # ============================================================

    @api.constrains('name', 'product_id', 'company_id')
    def _check_unique_serial_per_product(self):
        """منع تكرار نفس الـ Serial لنفس المنتج في نفس الشركة"""
        for lot in self:
            if lot.product_id.tracking != 'serial':
                continue
            duplicate = self.search([
                ('name',       '=', lot.name),
                ('product_id', '=', lot.product_id.id),
                ('company_id', '=', lot.company_id.id),
                ('id',         '!=', lot.id),
            ], limit=1)
            if duplicate:
                raise ValidationError(_(
                    'الرقم التسلسلي "%s" مسجل مسبقاً للمنتج "%s".\n'
                    'لا يمكن تكرار الأرقام التسلسلية.'
                ) % (lot.name, lot.product_id.display_name))

    # ============================================================
    #  Location Helpers
    # ============================================================

    @api.model
    def _get_pos_source_location(self, pos_config_id):
        """
        جلب الـ Source Location الخاص بنقطة البيع من الـ Picking Type.
        هذا هو الموقع الوحيد المسموح بالبيع منه.

        :param pos_config_id: int — معرّف إعداد POS
        :return: stock.location record أو False
        """
        if not pos_config_id:
            return False
        pos_config = self.env['pos.config'].browse(pos_config_id)
        picking_type = pos_config.picking_type_id
        if not picking_type:
            return False
        return picking_type.default_location_src_id or False

    def _get_qty_in_location(self, location):
        """
        إرجاع الكمية المتاحة للـ Serial في موقع محدد وأبنائه.

        :param location: stock.location record
        :return: float — الكمية المتاحة (إجمالي - محجوز)
        """
        self.ensure_one()
        if not location:
            return 0.0
        quants = self.env['stock.quant'].search([
            ('lot_id',      '=', self.id),
            ('location_id', '=', location.id),
            ('location_id.usage', '=', 'internal'),
        ])
        total    = sum(quants.mapped('quantity'))
        reserved = sum(quants.mapped('reserved_quantity'))
        return total - reserved

    def _get_qty_in_any_internal_location(self):
        """
        إرجاع الكمية الإجمالية في أي موقع داخلي.
        يُستخدم لتشخيص وجود الـ Serial في مخزن آخر.
        """
        self.ensure_one()
        quants = self.env['stock.quant'].search([
            ('lot_id', '=', self.id),
            ('location_id.usage', '=', 'internal'),
        ])
        total    = sum(quants.mapped('quantity'))
        reserved = sum(quants.mapped('reserved_quantity'))
        return total - reserved

    def _find_actual_locations(self):
        """
        إرجاع أسماء المواقع الفعلية التي يوجد فيها الـ Serial.
        يُستخدم لبناء رسالة خطأ واضحة للمستخدم.

        :return: list of str (أسماء المواقع)
        """
        self.ensure_one()
        quants = self.env['stock.quant'].search([
            ('lot_id', '=', self.id),
            ('location_id.usage', '=', 'internal'),
            ('quantity', '>', 0),
        ])
        return quants.mapped('location_id.complete_name')

    # ============================================================
    #  Main Validation Method
    # ============================================================

    @api.model
    def validate_serial_for_pos(self, serial_name, product_id, pos_config_id=None):
        """
        التحقق الكامل من الـ Serial قبل البيع في POS.

        ترتيب التحققات:
        ════════════════════════════════════════════════════════════
         1. المنتج يحتاج تتبع؟
         2. الـ Serial موجود في النظام؟
         3. حالته مش 'sold'؟
         4. ✨ موجود في موقع الـ POS تحديداً؟
            ├── لا  →  ❌ رسالة تقول "مش في موقع POS"
            │           + تذكر الموقع الفعلي لو موجود في مكان تاني
            └── نعم →  ✅ متاح للبيع
        ════════════════════════════════════════════════════════════

        :return: dict {
            valid:        bool,
            message:      str,
            lot_id:       int | False,
            status:       str,
            location_name: str | False,   ← اسم موقع الـ POS المطلوب
            actual_locations: list[str],  ← المواقع الفعلية للـ Serial
        }
        """
        product = self.env['product.product'].browse(product_id)

        # ── 1. منتج بدون تتبع ─────────────────────────────────
        if product.tracking not in ('serial', 'lot'):
          pass

        # ── 2. هل الـ Serial موجود في النظام؟ ─────────────────
        lot = self.search([
            ('name',       '=', serial_name),
            ('product_id', '=', product_id),
        ], limit=1)

        if not lot:
            return {
                'valid':            False,
                'message':          _('الرقم التسلسلي "%s" غير موجود في النظام.') % serial_name,
                'lot_id':           False,
                'status':           'not_found',
                'location_name':    False,
                'actual_locations': [],
            }

        # ── 3. هل حالته مباع ولم يُرتجع؟ ──────────────────────
        if lot.serial_status == 'sold':
            return {
                'valid':            False,
                'message':          _('الرقم التسلسلي "%s" مباع حالياً ولم يُرتجع بعد.') % serial_name,
                'lot_id':           lot.id,
                'status':           'sold',
                'location_name':    False,
                'actual_locations': [],
            }

        # ── 4. ✨ التحقق من الـ Location ───────────────────────
        pos_location = self._get_pos_source_location(pos_config_id)

        if pos_location:
            # ── الكمية في موقع الـ POS تحديداً ─────────────────
            qty_in_pos = lot._get_qty_in_location(pos_location)

            if qty_in_pos <= 0:
                # هل هو موجود في مخزن تاني؟
                actual_locations = lot._find_actual_locations()

                if actual_locations:
                    # موجود في مكان آخر — رسالة واضحة بالموقع الفعلي
                    locations_str = '، '.join(actual_locations)
                    message = _(
                        'الرقم التسلسلي "%s" غير موجود في موقع الـ POS "%s".\n'
                        'هو موجود حالياً في: %s'
                    ) % (serial_name, pos_location.complete_name, locations_str)
                else:
                    # مش موجود في أي مكان
                    message = _(
                        'الرقم التسلسلي "%s" غير متوفر في موقع الـ POS "%s".'
                    ) % (serial_name, pos_location.complete_name)

                return {
                    'valid':            False,
                    'message':          message,
                    'lot_id':           lot.id,
                    'status':           'wrong_location',
                    'location_name':    pos_location.complete_name,
                    'actual_locations': actual_locations,
                }

            # ✅ موجود في موقع الـ POS
            available_qty = qty_in_pos

        else:
            # مفيش pos_config — نتحقق من أي موقع داخلي
            available_qty = lot._get_qty_in_any_internal_location()
            pos_location  = None

            if available_qty <= 0:
                return {
                    'valid':            False,
                    'message':          _('الرقم التسلسلي "%s" غير متوفر في المخزن.') % serial_name,
                    'lot_id':           lot.id,
                    'status':           'out_of_stock',
                    'location_name':    False,
                    'actual_locations': [],
                }

        # ── ✅ متاح للبيع ──────────────────────────────────────
        location_name = pos_location.complete_name if pos_location else False

        if lot.serial_status == 'returned':
            msg = _(
                'الرقم التسلسلي "%s" مرتجع ومتاح للبيع مجدداً في "%s".'
            ) % (serial_name, location_name or _('المخزن'))
        else:
            msg = _(
                'الرقم التسلسلي "%s" متاح للبيع في "%s".'
            ) % (serial_name, location_name or _('المخزن'))

        return {
            'valid':            True,
            'message':          msg,
            'lot_id':           lot.id,
            'status':           lot.serial_status,
            'location_name':    location_name,
            'actual_locations': [location_name] if location_name else [],
        }

    # ── للاستخدام القديم (backward compat) ────────────────────
    def _get_available_qty_in_pos_location(self, pos_config_id=None):
        """Backward compatibility wrapper"""
        self.ensure_one()
        pos_location = self._get_pos_source_location(pos_config_id)
        if pos_location:
            return self._get_qty_in_location(pos_location)
        return self._get_qty_in_any_internal_location()

    # ============================================================
    #  State Transitions
    # ============================================================

    def action_mark_sold(self, order_line=None):
        self.ensure_one()
        self.write({'serial_status': 'sold'})
        if order_line:
            self.write({'pos_order_line_id': order_line.id})
        self.env['pos.serial.history'].create({
            'lot_id':        self.id,
            'order_id':      order_line.order_id.id if order_line else False,
            'order_line_id': order_line.id          if order_line else False,
            'operation':     'sale',
            'note':          _('بيع في POS'),
        })

    def action_mark_returned(self, order_line=None):
        self.ensure_one()
        new_return_count = self.return_count + 1
        self.write({
            'serial_status': 'returned',
            'return_count':  new_return_count,
        })
        self.env['pos.serial.history'].create({
            'lot_id':        self.id,
            'order_id':      order_line.order_id.id if order_line else False,
            'order_line_id': order_line.id          if order_line else False,
            'operation':     'return',
            'note':          _('مرتجع من POS — المرة رقم %d') % new_return_count,
        })

    def action_view_serial_history(self):
        return {
            'type':      'ir.actions.act_window',
            'name':      _('سجل الرقم التسلسلي: %s') % self.name,
            'res_model': 'pos.serial.history',
            'view_mode': 'list,form',
            'domain':    [('lot_id', '=', self.id)],
            'context':   {'default_lot_id': self.id},
        }

# POS Serial Validation — Odoo 18

موديل Odoo 18 للتحقق من الأرقام التسلسلية في مخزن نقطة البيع.

---

## هيكل الموديل

```
pos_serial_validation/
├── __manifest__.py
├── __init__.py
├── models/
│   ├── __init__.py
│   ├── stock_lot.py        ← منطق التحقق الرئيسي
│   └── pos_order.py        ← التحقق عند تأكيد الأوردر
├── controllers/
│   ├── __init__.py
│   └── pos_serial_controller.py   ← API endpoints للـ Frontend
├── static/src/
│   ├── js/
│   │   └── serial_validation.js   ← Patch على POS Frontend
│   └── xml/
│       └── serial_popup.xml       ← قوالب HTML
├── security/
│   └── ir.model.access.csv
└── views/
    └── pos_serial_validation_views.xml
```

---

## عمليات التحقق

### 1. منع تكرار Serial (Backend Constraint)
في `stock_lot.py` — `_check_unique_serial_per_product`:
- يمنع إنشاء نفس الرقم التسلسلي لنفس المنتج في نفس الشركة.

### 2. التحقق من وجود Serial في المخزن
في `stock_lot.py` — `validate_serial_for_pos()`:
- يتحقق من وجود الـ Serial في قاعدة البيانات.
- يتحقق من أنه لم يُباع مسبقاً.
- يتحقق من وجود كمية متاحة في موقع POS.

### 3. التحقق عند البيع في POS
- **Frontend (JS):** Patch على `EditListPopup` — يتحقق فوراً عند إدخال الرقم.
- **Backend:** Override على `_process_order` — تحقق نهائي قبل الحفظ.

---

## التثبيت

```bash
# انسخ المجلد إلى مسار الـ addons
cp -r pos_serial_validation /path/to/odoo/addons/

# حدّث قائمة التطبيقات
# Settings > Technical > Update Module List

# ثبّت الموديل
# Apps > Search: "POS Serial Validation" > Install
```

---

## الاستخدام

### من Backend:
```python
# التحقق من Serial يدوياً
result = env['stock.lot'].validate_serial_for_pos(
    serial_name='SN-001',
    product_id=42,
    pos_config_id=1,
)
# result = {'valid': True/False, 'message': '...', 'lot_id': 15}
```

### من Frontend (JS):
```javascript
const result = await this.orm.call(
    'stock.lot',
    'validate_serial_for_pos',
    ['SN-001', productId, posConfigId]
);
if (!result.valid) {
    // عرض رسالة الخطأ
}
```

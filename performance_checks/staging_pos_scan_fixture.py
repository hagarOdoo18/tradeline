"""Create a clearly labelled, synthetic POS fixture on neutralized staging only."""
import json
import os

assert os.environ.get('ODOO_STAGE') == 'staging', 'STAGING ONLY'
assert env.cr.dbname == os.environ['PGDATABASE']
assert env['ir.config_parameter'].sudo().get_param('database.is_neutralized') == 'true'
marker = 'QA PERF POS 20261021'
assert not env['pos.config'].search_count([('name', '=', marker)]), 'Fixture already exists'
env = env(context=dict(env.context, no_reset_password=True, tracking_disable=True,
                       mail_create_nolog=True, shopify_no_export=True))
base = env['pos.config'].search([('company_id', '=', env.company.id)], limit=1)
assert base and base.picking_type_id.default_location_src_id
config = base.copy({'name': marker, 'cash_control': False,
                    'payment_method_ids': [(6, 0, base.payment_method_ids.ids)]})
if 'module_pos_hr' in config._fields:
    config.module_pos_hr = False
if 'enable_product_bar_lot_serial_search' in config._fields:
    config.enable_product_bar_lot_serial_search = True
location = config.picking_type_id.default_location_src_id
assert not env['product.product'].search_count([
    ('barcode', 'in', ['9900026102101', '9900026102102'])
]), 'Reserved QA barcodes already exist'
category = env['product.category'].create({
    'name': marker, 'property_valuation': 'manual_periodic',
    'property_cost_method': 'standard',
})
products = env['product.product'].create([
    {'name': marker + ' Serial', 'default_code': 'QA-PERF-SERIAL-PRODUCT',
     'barcode': '9900026102101', 'tracking': 'serial', 'is_storable': True,
     'available_in_pos': True, 'categ_id': category.id,
     'list_price': 10, 'taxes_id': [(5, 0, 0)]},
    {'name': marker + ' Plain', 'default_code': 'QA-PERF-PLAIN-PRODUCT',
     'barcode': '9900026102102', 'tracking': 'none', 'is_storable': True,
     'available_in_pos': True, 'categ_id': category.id,
     'list_price': 5, 'taxes_id': [(5, 0, 0)]},
])
lots = env['stock.lot'].create([
    {'name': name, 'product_id': products[0].id, 'company_id': env.company.id}
    for name in ['9900026102111', '9900026102112']
])
for lot in lots:
    env['stock.quant']._update_available_quantity(products[0], location, 1, lot_id=lot)
env['stock.quant']._update_available_quantity(products[1], location, 5)
session = env['pos.session'].create({'config_id': config.id, 'user_id': env.user.id})
env.flush_all()
env.cr.commit()
print('STAGING_POS_FIXTURE ' + json.dumps({
    'config_id': config.id, 'session_id': session.id, 'location_id': location.id,
    'product_ids': products.ids, 'serials': lots.mapped('name'),
    'plain_barcode': products[1].barcode, 'name': marker,
}), flush=True)

from odoo import SUPERUSER_ID, api

SERVICE_SCRAP_OPERATION_NAME = 'Service Scrap Location'
SERVICE_VENDOR_OPERATION_NAME = 'Service Vendor Location'

WAREHOUSE_LOCATION_CONFIG = {
    'SER-W': {
        'fallback_name': 'Service Warehouse',
        'vendor_location_name': 'Vendor Scrapped',
        'scrap_location_name': 'Service Scrapped',
    },
    'SW-XP': {
        'fallback_name': 'Service Warehouse XPRS',
        'vendor_location_name': 'Vendor Scrapped XPRS',
        'scrap_location_name': 'Service Scrapped XPRS',
    },
}


def _find_virtual_locations_parent(env):
    parent = env['stock.location'].search([
        ('complete_name', '=', 'Virtual Locations')
    ], limit=1)
    if parent:
        return parent
    return env['stock.location'].search([
        ('name', '=', 'Virtual Locations'),
        ('usage', '=', 'view'),
    ], limit=1)


def _find_warehouse(env, code, fallback_name):
    warehouse = env['stock.warehouse'].search([('code', '=', code)], limit=1)
    if warehouse:
        return warehouse
    return env['stock.warehouse'].search([('name', '=', fallback_name)], limit=1)


def _ensure_scrap_location(env, warehouse, name, is_vendor, parent_location):
    domain = [
        ('name', '=', name),
        ('usage', '=', 'inventory'),
        ('company_id', '=', warehouse.company_id.id),
    ]
    if parent_location:
        domain.append(('location_id', '=', parent_location.id))

    location = env['stock.location'].search(domain, limit=1)
    vals = {
        'name': name,
        'usage': 'inventory',
        'company_id': warehouse.company_id.id,
        'scrap_location': True,
        'scrap_vendor_location': is_vendor,
    }
    if parent_location:
        vals['location_id'] = parent_location.id

    if location:
        location.write({
            'scrap_location': True,
            'scrap_vendor_location': is_vendor,
        })
        return location

    return env['stock.location'].create(vals)


def _sequence_code(warehouse_code, suffix):
    compact = (warehouse_code or '').replace('-', '').upper()
    return f'{compact}_{suffix}'


def _ensure_operation_type(env, warehouse, name, dest_location, suffix):
    picking_type = env['stock.picking.type'].search([
        ('warehouse_id', '=', warehouse.id),
        ('code', '=', 'internal'),
        ('name', '=', name),
    ], limit=1)

    vals = {
        'name': name,
        'code': 'internal',
        'active': True,
        'warehouse_id': warehouse.id,
        'company_id': warehouse.company_id.id,
        'sequence_code': _sequence_code(warehouse.code, suffix),
        'default_location_src_id': warehouse.lot_stock_id.id,
        'default_location_dest_id': dest_location.id,
    }

    if picking_type:
        picking_type.write(vals)
        return picking_type

    return env['stock.picking.type'].create(vals)


def post_init_hook(cr, registry):
    env = api.Environment(cr, SUPERUSER_ID, {})
    parent_location = _find_virtual_locations_parent(env)

    for warehouse_code, cfg in WAREHOUSE_LOCATION_CONFIG.items():
        warehouse = _find_warehouse(env, warehouse_code, cfg['fallback_name'])
        if not warehouse:
            continue

        vendor_location = _ensure_scrap_location(
            env,
            warehouse,
            cfg['vendor_location_name'],
            is_vendor=True,
            parent_location=parent_location,
        )
        scrap_location = _ensure_scrap_location(
            env,
            warehouse,
            cfg['scrap_location_name'],
            is_vendor=False,
            parent_location=parent_location,
        )

        _ensure_operation_type(
            env,
            warehouse,
            SERVICE_VENDOR_OPERATION_NAME,
            vendor_location,
            'VENDOR',
        )
        _ensure_operation_type(
            env,
            warehouse,
            SERVICE_SCRAP_OPERATION_NAME,
            scrap_location,
            'SCRAP',
        )

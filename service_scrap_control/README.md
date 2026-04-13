# Service Scrap Control (Odoo 18)

This module ports the Odoo12 Service scrap workflow into Odoo18 for:
- Service Warehouse (`SER-W`)
- Service Warehouse XPRS (`SW-XP`)

## What It Adds

- Picking workflow fields: `request_scrap`, `wait_approve_scrap`, `approve_scrap`
- Picking workflow actions: `Request Scrap`, `Approve Scrap`, `Vendor Scrap`
- Service/XPRS scrap wizard models:
  - `request.scrap.wizard`
  - `stock.scrap.wizard`
  - `scrap.line`
- Scrap approval states on `stock.scrap`:
  - `draft -> witting -> approve -> done`
- `stock.location.scrap_vendor_location`
- `stock.picking.type.user_ids` restriction field
- Security groups with umbrella group: `Service Scrap Control`

## Maintain Access (Add/Remove Users)

Primary path (recommended):
1. Go to **Settings > Users & Companies > Users**.
2. Open user.
3. In Access Rights, add/remove **Service Scrap Control** group.

Advanced path (per operation type):
1. Go to **Inventory > Configuration > Operation Types**.
2. Open operation type.
3. Edit **Users** field to override allowed users.

## Install Notes

- On module install, post-init hook creates/updates Service and XPRS scrap locations and operation types:
  - `Service Scrap Location`
  - `Service Vendor Location`
- Menu access for **Inventory > Operations > Scrap** is extended with `Scrap Inventory` capability group.

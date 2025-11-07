# Stock: Force New Picking (Odoo 18)

This module forces Odoo to always create a **new** Stock Picking for new stock moves
instead of reusing any existing open pickings (draft / waiting / ready) with the same
route (locations, picking type, company).

## How it works
Overrides `stock.move._search_picking_for_assign` to return an empty recordset, which
forces the stock engine to create a new picking.

## Install
1. Copy the folder `stock_force_new_picking` into your Odoo addons path.
2. Update Apps list.
3. Install **Stock: Force New Picking**.
4. Try any transfer A → B even if an open picking exists; a **new picking** will be created.

## Notes
- No security or views are added.
- Safe to uninstall; it only overrides a method, no data model changes.

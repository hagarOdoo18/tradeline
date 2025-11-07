# POS Serial Search by SN (Odoo 18)

Enables searching products by **Serial Number (stock.production.lot.name)** directly in POS.
- Control button "Search Serial" opens a prompt to enter the SN and adds the product.
- Optional patch: typing or scanning the SN in the normal search bar adds the product instantly.

## Requirements
- Inventory > Settings: enable **Lots & Serial Numbers**.
- For products you want to find by SN: Inventory tab > **Tracking = By Unique Serial Number**.
- Ensure the products are **available in POS**.

## Install
1. Copy `pos_serial_search_by_sn` to your addons path.
2. Update Apps list and install (or -u the module).
3. Reload POS session.

## Notes
- The module builds an index at POS load time. Reload the POS after creating new serials.
- If you have a very large number of serials, consider filtering the search domain by company or category inside `serial_loader.js`.

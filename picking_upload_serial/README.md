# picking_upload_serial

Module to upload an Excel (.xlsx) file to a Stock Picking (Delivery).

**Excel columns (header row required):**
- Code
- Serial
- Quantity

**Features**
- Supports serial-tracked and non-serial products
- Option to create serial (lot) if not exist
- Option to auto-confirm (validate) the picking
- Button on picking form 'Upload Excel'

**Installation**
- Drop this module into your addons folder
- Update apps list and install
- Requires `openpyxl` Python package on the Odoo server

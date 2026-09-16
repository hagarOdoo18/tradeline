# Pricelist Import Wizard — Odoo 18

## Overview
Adds an **Import Items** button on every Pricelist form. Opens a multi-step
wizard that parses an Excel or CSV file and bulk-creates/updates
`product.pricelist.item` records.

---

## Features
| Feature | Value |
|---|---|
| `applied_on` | Always `1_product` (Product level) |
| `compute_price` | Always `fixed` |
| `currency_id` | Inherited from the pricelist |
| `company_id` | Inherited from the pricelist |
| File formats | `.xlsx`, `.xls`, `.csv` |
| Duplicate handling | Optional overwrite (checkbox) |
| Preview step | Shows resolved products + validation errors before committing |

---

## Required Spreadsheet Format

| item_code | fixed_price |
|-----------|-------------|
| PROD-001  | 29.99       |
| PROD-002  | 149.00      |

- **`item_code`** must match a product's **Internal Reference** (`default_code`).
- **`fixed_price`** must be a numeric value.
- Row 1 must be the **header** row.
- Accepted column name aliases:
  - `item_code`: `internal_reference`, `default_code`, `product_code`, `code`, `ref`, `reference`
  - `fixed_price`: `price`, `unit_price`, `sales_price`, `list_price`, `sale_price`

---

## Wizard Flow

```
Draft  ──[Preview]──►  Preview  ──[Import]──►  Done
                          │
                          └── [Reset] ──► Draft
```

1. **Draft** — upload file, choose optional date range / overwrite flag.
2. **Preview** — all rows shown with status badges (`Ready` / `Error`).
   Rows coloured in red will be skipped during import.
3. **Done** — summary shows how many items were created vs updated.

---

## Installation

```bash
# Copy module to your addons path
cp -r pricelist_import_wizard /path/to/odoo/custom_addons/

# Restart Odoo and update apps list, then install:
# Settings → Apps → search "Pricelist Import" → Install
```

### Python dependencies
```bash
pip install openpyxl   # for .xlsx files
pip install xlrd       # for legacy .xls files  (optional)
```
Both are usually pre-installed in standard Odoo environments.

---

## Module Structure

```
pricelist_import_wizard/
├── __init__.py
├── __manifest__.py
├── security/
│   └── ir.model.access.csv
├── views/
│   ├── pricelist_import_wizard_view.xml   # Wizard form
│   └── product_pricelist_view.xml         # "Import Items" button on pricelist
└── wizard/
    ├── __init__.py
    └── pricelist_import_wizard.py         # TransientModel + line model
```

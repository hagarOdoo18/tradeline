# Pricelist Import Wizard — Odoo 18

Adds an **Import Items** button on every Pricelist form. The wizard parses an
Excel or CSV file and bulk-creates/updates `product.pricelist.item` records.

Both driving fields are fully honoured: **Apply On** (`applied_on`) decides what
each rule targets, **Compute Price** (`compute_price`) decides how its price is
computed. The two choices together decide which columns the sheet must contain —
the wizard shows that list live in the **Sheet must contain** box.

---

## Apply On → target column

| Apply On | `applied_on` | Sheet column | Resolved against |
|---|---|---|---|
| All Products | `3_global` | *(none)* | — one rule per sheet row (use `min_quantity` for tiers) |
| Product Category | `2_product_category` | `category` | `complete_name` exact → `name` exact → `complete_name ilike` |
| Product | `1_product` | `item_code` | template `barcode` → template `default_code` → variant barcode/ref → its template |
| Product Variant | `0_product_variant` | `item_code` | variant `barcode` → variant `default_code` |

## Compute Price → value column(s)

| Compute Price | `compute_price` | Required in sheet | Extra wizard fields |
|---|---|---|---|
| Fixed Price | `fixed` | `fixed_price` | — |
| Discount | `percentage` | `percent_price` | — |
| Formula | `formula` | at least one of `price_discount`, `price_surcharge`, `price_round`, `price_min_margin`, `price_max_margin` | **Based on**: Sales Price / Cost / Other Pricelist (+ the other pricelist) |

`min_quantity` is optional in every mode and defaults to `0`.

Values the chosen mode does not use are written as `0.0`, so re-importing over an
existing rule never leaves a stale price from a previous mode. `base` and
`base_pricelist_id` are likewise reset outside Formula mode.

---

## Accepted column names

Headers are matched case-insensitively, with spaces and hyphens treated as `_`.

| Canonical | Aliases |
|---|---|
| `item_code` | `internal_reference`, `default_code`, `product_code`, `code`, `ref`, `reference`, `barcode`, `sku`, `product` |
| `category` | `categ`, `categ_id`, `product_category`, `category_name`, `categ_name` |
| `min_quantity` | `min_qty`, `minimum_quantity`, `qty`, `quantity` |
| `fixed_price` | `price`, `unit_price`, `sales_price`, `list_price`, `sale_price`, `new_price` |
| `percent_price` | `percentage`, `discount`, `percent`, `discount_percent`, `percentage_price` |
| `price_discount` | `formula_discount`, `discount_formula`, `discount`, `percentage` |
| `price_surcharge` | `surcharge`, `extra_fee`, `extra` |
| `price_round` | `rounding`, `round`, `rounding_method` |
| `price_min_margin` | `min_margin`, `minimum_margin` |
| `price_max_margin` | `max_margin`, `maximum_margin` |

Number parsing handles `1.234,56`, `1,234.56`, `29,99`, a trailing `%`, and
barcodes Excel turned into `1234567890.0`.

---

## Example sheets

**Product + Fixed Price**

| item_code | fixed_price | min_quantity |
|---|---|---|
| 6221031492018 | 29.99 | 0 |
| 6221031492025 | 27.50 | 10 |

**Product Category + Discount**

| category | percent_price |
|---|---|
| All / Saleable / Phones | 15 |

**All Products + Formula** (10% off Sales Price, rounded to 0.05)

| price_discount | price_round |
|---|---|
| 10 | 0.05 |

---

## Wizard flow

```
Draft  ──[Preview]──►  Preview  ──[Import]──►  Done
                          │
                          └── [Reset] ──► Draft
```

1. **Draft** — pick Apply On, Compute Price, validity dates and the overwrite
   flag, then upload the file.
2. **Preview** — every row is listed with its sheet row number, resolved target
   and a status badge:
   - **Ready** — will be imported
   - **Warning** — several records match the code; the first is used, and the
     row is still imported
   - **Error** — target not found, or no value in the expected column; the row
     is skipped
   The preview table shows only the value columns relevant to the chosen mode.
3. **Done** — summary of created / updated / skipped counts.

### Duplicate handling

A row matches an existing item when `pricelist_id`, `applied_on`, the target
(`product_tmpl_id` / `product_id` / `categ_id`) and `min_quantity` are all equal.
With **Overwrite Existing Items** checked the match is updated; unchecked, the
row is counted as skipped.

---

## Module structure

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
    └── pricelist_import_wizard.py         # TransientModel + preview line model
```

### Python dependencies

```bash
pip install openpyxl   # .xlsx
pip install xlrd       # legacy .xls (optional)
```

Both are normally present in a standard Odoo environment. CSV needs nothing.

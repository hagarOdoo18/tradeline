# sale_allow_below_cost — Odoo 18

## Purpose
Centralized configuration that controls whether users may sell products
**below their cost price** in both **Sales Orders** and **Point of Sale**.

---

## Features

| Feature | Details |
|---|---|
| Central toggle | Single on/off switch in **Settings → Sales** and **Settings → Point of Sale** |
| Date range | Optional *From* / *To* dates. Outside the range → blocked automatically |
| Action | **Warning** (allow save with orange highlight) or **Block** (prevent save) |
| Sale Order | Orange row highlight + banner + server-side validation on save & confirm |
| POS | Real-time notification when cashier sets price below cost |

---

## Installation

1. Copy the `sale_allow_below_cost` folder into your Odoo `addons` path.
2. Restart the Odoo server.
3. Go to **Apps**, search for *Allow Sell Below Cost*, and click **Install**.

---

## Configuration

Navigate to **Settings → Sales → Orders** (or **Settings → Point of Sale**):

```
☑  Allow Selling Below Cost Price
        Allowed From  [  2024-01-01  ]
        Allowed To    [  2024-12-31  ]
        When Below Cost  [Warning ▾]
```

| Setting | Behaviour |
|---|---|
| Toggle OFF | Any below-cost price on Sale Order or POS is **blocked** |
| Toggle ON, no dates | Below-cost selling always allowed within chosen action |
| Toggle ON, with dates | Only allowed within the date window |
| Action = Warning | Orange highlight; order can still be saved/confirmed |
| Action = Block | Error raised; order cannot be saved/confirmed |

---

## Technical Notes

* Settings are stored as `ir.config_parameter` (system parameters), so they
  are company-independent by default. Extend `res.company` fields if you need
  per-company control.
* The POS fetches the config via `pos.config.get_below_cost_config()` RPC on
  session startup.  
* `standard_price` is loaded into the POS product model via the session
  loader override in `PosSession._loader_params_product_product`.

---

## Dependencies

* `sale_management`
* `point_of_sale`
* `base_setup`

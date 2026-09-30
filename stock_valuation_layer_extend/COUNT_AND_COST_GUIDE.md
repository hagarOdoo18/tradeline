# Controlled Count & Cost Adjustment

After deploying and upgrading `stock_valuation_layer_extend`, give selected staff the **Inventory / Count Stock and Set Cost** group. Members can use **Inventory → Operations → Count & Cost Adjustment** and view **Inventory Issues** and **Controlled Stock Adjustments** history. Give only approved cost editors the additional **Inventory Cost Approver** group. The group includes Odoo's Inventory User access; review each assigned user's other stock groups and menus separately.

## Staff workflow

1. Select the active company in Odoo's company menu. Open **Count & Cost Adjustment**.
2. Select one storable product and one internal location. For a tracked product, select the exact existing lot or serial number. Use an internal transfer if that serial is actually at another location.
3. Enter the **counted quantity**, not the quantity to add or remove. A serial count must be 0 or 1.
4. If stock is being added, tick **Set Unit Cost** and enter a positive unit cost. For an existing valued lot/serial, this is the desired cost of the remaining lot. For an untracked AVCO product, it is the desired average cost of all company stock of that product, so it may revalue units in other locations. A cost-only change can also be made without changing quantity.
5. State the physical reason and the **cost source/reference** (PO, bill, or approved opening-stock estimate). Do not present an estimate as an invoice price.
6. Click **Preview** and check the current location quantity, quantity change, and company valuation before posting. Click **Apply Quantity & Cost** once. The result shows the final company quantity, value, and cost, and links to a permanent audit record.

## What the wizard does

- It requires the dedicated group and the active company, rejects negative counts/costs, serial duplicates in another internal/transit location, reserved stock being changed, packaged/owned stock, and products with an existing physical-versus-valuation quantity gap.
- It applies a count through `stock.quant.inventory_quantity` and `action_apply_inventory`, so Odoo creates the normal stock move and valuation layer. It then applies any requested lot or product revaluation through Odoo's standard cost write, which creates the associated valuation and accounting entries.
- It checks that the current balance has not changed since Preview. A conflict stops the transaction, requiring a new preview.
- It stores the operator, reason, cost source, before/after balances, and resulting valuation-layer IDs in **Controlled Stock Adjustments**. The application happens in one database transaction; an error rolls back the attempted changes.

## Scope

The first version handles **one product and one internal location per adjustment**. It supports serial, lot, and untracked products for quantity counts. Cost entry supports automated average-cost products; tracked products must also use lot/serial valuation. It does not handle consigned or packaged stock, transfers, FIFO cost edits, purchase receipts, supplier invoices, or bulk spreadsheets. Use their normal Odoo workflows for those cases.

The wizard does not repair an already mismatched product or replace Finance's review of unsupported historical costs. Its menu and server methods are restricted to the dedicated group; other Odoo stock permissions and existing adjustment tools still require an access review if staff should no longer use them.

## Production release and verification

This change is based on production commit `5eec462`; the inventory commits from the development branch were applied selectively. Production data has not been modified.

Upgrade `stock_valuation_layer_extend`, `tradeline_update_product_qty`, `import_stock_quant_excel`, and `stock_multi_update` together on a copy of production first. The three quantity tools use the same guarded count posting and audit records. Excel and Multi Stock Update retain their add/subtract meaning; Update Product Quantity and the new wizard accept final physical counts. Transit quantities are shown separately in Valuation by Product and included when checking quantity gaps and correction candidates.

Run Odoo with `--test-enable --test-tags /stock_valuation_layer_extend --stop-after-init` against that isolated database. Verify a serial receipt/count, removal, cost-only repair, stale preview, duplicate serial, reserved stock, accounting posting, and repeated Excel submission. Assign test users the staff group and approver group separately and verify cost editing is denied without approval access. Local Python/XML validation does not replace these Odoo database tests.

Inventory Issues is a live read-only view of product and valued-lot balances. It includes physical stock in internal and transit locations, excludes consignment, and reports quantity gaps, negative stock, duplicate serials, value without stock, zero/negative costs, and AVCO stored-cost discrepancies. Corrections with historical quantity gaps require a manager review of moves and valuation layers; the count wizard deliberately blocks them rather than inventing missing costs. Multiple problematic serial costs can be corrected individually when quantities reconcile.

For the Marshall investigation, current quantities reconciled (14 internal plus 1 transit, 15 valued). The remaining in-stock serial `73400553E319A8B0070179` had valuation cost 12,464.05 but stored outgoing cost zero. A cost-only adjustment using a verified cost source is required before its next quantity change. This code does not post that correction automatically. The historical missing opening movements and later negative AVCO were observed; the precise original UI action cannot be established from the retained records.

The controls cover these adjustment tools. Users retaining manager access, external integrations, or other custom tools can still bypass them; review those permissions before rolling out. Use a backup and staging verification before upgrading production.

Local verification: Python syntax, XML parsing, ACL CSV structure, manifest file references, and PostgreSQL syntax for all three report views and the company migration passed. Five isolated preview checks passed (cached-cost repair, valuation-cost repair, no-op rejection, unknown-cost addition rejection, and company-wide AVCO projection). The Odoo integration suite is present but has not run locally because this workspace does not contain an Odoo runtime or database.

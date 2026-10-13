# Excel stock import

Open **Inventory → Products → Import Stock (Excel)**. The XLSX workbook uses
the existing column order: **Location, Item Code, Quantity, Serial**. Item Code
matches the product barcode. Quantity is a signed change: `1` adds one unit and
`-1` removes one unit; it is not the final counted quantity.

1. Upload the file and click **Preview**.
2. Use **Create Lots** for missing serials, or correct/remove invalid rows.
3. Click **Apply** once. Up to 100 rows are posted immediately. Larger files
   are saved and processed automatically in batches.
4. For a large file, close the dialog and open **Products → Stock Import
   History**. Open the import and click **Refresh** to see processed, skipped
   and remaining rows. **Done** means every row has been handled.

Positive serial additions that are already in stock, or repeated in the file,
are skipped and explained in **Result / Note**. A serial at another location
requires an internal transfer, rather than another stock addition. Signed
removals still remove stock. Repeated untracked rows are combined by product
and location; they are not discarded as duplicate serials.

Each posted batch uses Odoo's inventory adjustment action, creating stock
moves and native valuation/accounting entries together. The existing product
or serial cost is used, including zero cost. Historical valuation gaps and
cost issues neither block imports nor get repaired automatically; they remain
available in Inventory Issues for a separate review.

If a background batch fails, that batch is rolled back and the import shows
**Needs Review** with its error. Earlier completed batches remain saved. After
addressing the error, **Retry Remaining Rows** resumes the same import without
reposting completed rows. Do not re-upload the whole file for untracked
products: those rows represent signed additions and a new import would add
them again. If physical stock changed after Preview, inspect the affected
rows before continuing; Retry preserves the original stock snapshots.

The **Import Stock Quant** role grants access to this tool. Staff see their
own import history in allowed companies; administrators can review all imports
in their allowed companies. The cron processes each batch as the user who
created the import, using the existing import role and stock read permissions.

Regression tests cover native stock/valuation/accounting posting, zero costs
with pre-existing valuation gaps, serial re-uploads, duplicate rows, stock
changes after Preview, queued batches, and rollback/resume. Large-file tests
are tagged `stock_import_large` so routine upgrades do not post thousands of
test rows. Run them on an isolated test database.

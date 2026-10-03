# Reconcile valuation to verified physical stock

Open Inventory > Products > Inventory Issues and click **Reconcile Valuation** on a quantity mismatch. For a valued tracked product, use the individual lot/serial row, not the product total. The operator needs the existing **Neutralize Erroneous Valuation Quantity** role (inventory and accounting manager access). Count operators alone cannot post this repair.

The target is the recorded company-owned physical quantity across all internal **and transit** locations. Verify this stock independently and enter the count/evidence reference. Do not enter a signed difference or a new warehouse count: the form calculates the difference and leaves all physical quantities and reservations unchanged.

Examples:

* Physical 3, valued 1: correction quantity +2, target valuation quantity 3.
* Physical 2, valued 5: correction quantity -3, target valuation quantity 2.
* A serial physically present has target quantity 1; an absent serial has target 0. Duplicate or negative physical stock must be investigated first.

Choose the value policy deliberately:

* **Keep existing total value** if accounting value is already correct and only quantity history is wrong. Physical 3, valued 1, total value 300 results in quantity 3, total value 300, cost per unit 100. No journal entry is needed for a zero-value correction.
* **Set verified cost per unit** if the value is also wrong. Physical 3, valued 1, total value 100, verified unit cost 100 results in quantity 3 and value 300. The +200 value change debits the stock valuation account and credits the chosen counterpart account. A decrease reverses these signs. An accountant must select the appropriate correction account.

Confirm the recorded physical stock, give a reason/reference, click Preview and review quantity, value and cost. Then Reconcile Valuation Only posts the additive correction layer and any balanced journal entry in one transaction. Stock/value changes after Preview invalidate it. The form cannot be applied twice.

The corrected cost cache is aligned without creating a second revaluation. See Inventory > Products > **Valuation Reconciliation History** for operator, evidence, prior balances, correction layer and journal. Refresh Inventory Issues after posting. Other cost or stock problems can remain even after the selected quantity gap is resolved.

Scope: automated average-cost valuation only. Products valued per serial/lot are repaired per lot. Serial-tracked products valued at product level are repaired using the product total, after checking every physical serial for duplicates and missing serial numbers. This is a present-balance repair. It does not reconstruct historical receipts, rewrite past delivery cost/COGS, or restore FIFO remaining layers. Historical accounting differences need separate review.

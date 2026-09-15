# iPhone Pre-order Investigation and Staging Rollout

## Current iPhone 17 workflow found in the code

1. The pre-order is a normal `sale.order` in draft/sent state with `inv_type = quotation`.
2. The quotation contains a product whose name/description includes `Down Payment`.
3. `branch` registers one or more inbound `account.payment` records directly on that quotation through `account.payment.sale_order_id`.
4. `accounting_customization` deliberately blocks confirming a Down Payment quotation into a sales order.
5. At delivery, staff create a second sales/POS document. The POS helper copies the customer, Down Payment line, source quotation number, and reference, but it does not move or reconcile the original payment.
6. The payment therefore remains an unreconciled customer credit attached to the source quotation. The separate `sale_payment_return` module creates an outbound payment, after which staff take payment again on the delivery invoice.

This explains the duplicate refund/recharge work. The issue is not missing source references; those are already copied to POS/invoices. The missing operation is applying the original posted customer credit to the final delivery invoice.

Two related Odoo 18 defects were also found in the current modules:

- `branch._compute_amount_paid` checks for `account.payment.state == 'posted'`, but Odoo 18 payment states are `draft`, `in_process`, `paid`, `canceled`, and `rejected`. The posted status belongs to `payment.move_id.state`, so the Sales Order **Amount Paid** display can be wrong even when a valid posted payment exists.
- `sale_payment_return` only treats `payment.state == 'paid'` as returnable. A valid payment using an outstanding account can be `in_process` while its journal entry is posted, so some payments may be omitted from the return button.

The new pre-order view deliberately uses the posted journal entry (`payment.move_id.state == 'posted'`) as the accounting source of truth, and it excludes any original payment that already has a posted outbound return.

## New staged workflow

The `preorder_management` module is isolated from the existing flow and is intended to be installed on staging first.

1. A central manager creates an **iPhone 18** campaign, selects the date range, product variants, and participating branches.
2. The central manager enters a product quota for every branch in the **Branch Allocation** grid and opens the campaign.
3. The active branch manager creates the **Customer Pre-order first** from `Sales > Pre-orders > Customer Pre-orders`. Customer, Sales Rep, requested product/quantity, delivery price/discount, Discount Reason, and Deposit Amount are entered there. Branch and date default automatically.
4. **Create Sales Order** generates the Down Payment quotation. Its existing Sales Order `Reference Number` is filled automatically with the Customer Pre-order sequence (for example `PRE/2026/00002`), so branch staff never type a pre-order reference.
5. **Register Payment** is launched from the Customer Pre-order and posts the deposit against that generated quotation. The branch manager chooses the payment journal, actual collection date, and amount, posts the payment, returns to the Customer Pre-order, and clicks **Confirm Pre-order**. Confirmation is allowed only after a reusable posted payment exists.
6. The central manager starts allocation and allocates each paid customer request. Allocation is transaction-locked so two admins cannot consume the same last unit.
   Once a quotation is linked to an active pre-order record, it is removed from the legacy Sales/POS Down Payment selectors so staff cannot accidentally process it through both workflows.
7. The central manager starts delivery, creates the draft delivery sales order from the allocated pre-order, confirms it, and the branch validates the serial-controlled stock delivery.
8. **Invoice & Apply Original Payment** creates/posts the delivery invoice, changes the original posted payment journal date to the delivery invoice accounting date, reposts it, and reconciles its open receivable line. It does not create an outbound refund or a second inbound payment.
9. If the prepayment is smaller than the invoice, the record moves to **Payment Due** and shows the remaining invoice balance. If it fully settles the invoice, it moves to **Completed**.

### Optional POS final delivery (staging only)

The final delivery step can now run from the branch POS without creating a POS order or a second payment. It is disabled on every POS by default.

1. Enable **Pre-order Delivery** on one staging POS configuration while its session is closed, then reopen the session so the new setting and assets load.
2. In POS, open **Actions > Pre-order Delivery**. The dialog only lists fully paid, allocated pre-orders for that POS branch while the campaign is in Delivery.
3. Select the customer, review campaign/customer notes and reserved devices, then scan each required serial number.
4. **Deliver & Invoice** confirms the normal delivery sales order, validates stock from the exact POS source location, posts the invoice, and applies the original payment in one transaction.
5. If any serial, stock, accounting, or reconciliation check fails, the whole operation rolls back. Retrying the same request cannot deliver it twice.

Phase one deliberately blocks partial or overpaid pre-orders, lot-tracked products, split stock routes, offline use, and a mismatch between the POS source location and the pre-order warehouse. Those cases remain available through the Sales workflow.

Returned payments are identified and blocked from reuse.

The branch initially records the real deposit collection date. At delivery, the system temporarily returns the original payment to draft, changes its journal date to the posted delivery invoice accounting date, reposts the same payment, and then reconciles it to the invoice. If the payment is already reconciled, hash-protected, or belongs to a locked accounting period, delivery is blocked for Accounting review instead of changing it silently.

## Staging UAT

1. Refresh staging from production and deploy this branch.
2. Update the Apps list and install **Tradeline Pre-order Management** only on staging.
3. Grant **Pre-order Management / Central Admin** to the central allocation admins and **Branch Manager** to participating branch managers.
4. Create an `iPhone 18 Staging` campaign with the test product variants and branch quotas. Open the campaign, then create fresh Customer Pre-orders from the assigned branch accounts. Verify **Create Sales Order** generates each quotation with the Customer Pre-order number as its Reference Number.
5. Select at least these test cases:
   - one full payment in a single journal;
   - one split payment across two journals;
   - one partial deposit with a balance due;
   - one already-returned payment (must be blocked);
   - two customers competing for the last unit in a branch quota;
   - a serial-tracked iPhone delivery;
   - POS delivery with a valid serial in the configured POS source location;
   - POS delivery with a wrong-location, unknown, duplicate, and already-sold serial (all must be blocked with no partial delivery or invoice);
   - a repeated POS confirmation/retry (must create only one delivery and one invoice);
   - a discount-reason quotation.
6. For successful cases, verify that the original `account.payment` IDs and journals are unchanged, their dates now match the delivery invoice accounting date, no outbound return is created, no second inbound payment is created, and the final invoice shows the original payment in its reconciled payments.
7. Reconcile the staging accounting entries and compare customer partner ledgers before/after. The pre-order credit should move from outstanding to the delivery invoice with no net cash movement.
8. Confirm no `pos.order`, `pos.payment`, second inbound payment, or outbound refund was created by the POS delivery action.

## Production gate

Do not install in production until Finance signs off on the partner-ledger result, Retail signs off on serial delivery, and staging confirms that the payment journals have configured outstanding accounts. Any payment on a different receivable/outstanding account is deliberately blocked for Accounting review instead of being silently reclassified.

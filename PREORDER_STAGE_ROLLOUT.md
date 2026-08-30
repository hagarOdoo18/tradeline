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

1. A manager creates an **iPhone 18** campaign, selects the date range, product variants, and participating branches.
2. The manager enters a product quota for every branch in the **Branch Allocation** grid.
3. A salesperson opens the customer's paid Down Payment quotation and clicks **Create Pre-order** inside the Sales Order form.
4. The pre-order list exposes the required fields: Customer, Branch, Date, Sales Rep, and Discount Reason. There is no separate pre-order reference field; the existing Sales Order number remains the source link.
5. The manager confirms and allocates each customer request. Allocation is transaction-locked so two admins cannot consume the same last unit.
   Once a quotation is linked to an active pre-order record, it is removed from the legacy Sales/POS Down Payment selectors so staff cannot accidentally process it through both workflows.
6. During delivery, the branch creates a draft delivery sales order from the pre-order record, confirms it, and validates the serial-controlled stock delivery.
7. **Invoice & Apply Original Payment** creates/posts the delivery invoice and reconciles the original payment's open receivable line. It does not create an outbound refund or a second inbound payment.
8. If the prepayment is smaller than the invoice, the record moves to **Payment Due** and shows the remaining invoice balance. If it fully settles the invoice, it moves to **Completed**.

Returned payments are identified and blocked from reuse.

## Staging UAT

1. Refresh staging from production and deploy this branch.
2. Update the Apps list and install **Tradeline Pre-order Management** only on staging.
3. Grant **Pre-order Management / Manager** to the central allocation admins and **User** to participating branch staff.
4. Create an `iPhone 18 Staging` campaign with the test product variants and branch quotas. Create fresh staging Down Payment quotations and click **Create Pre-order** from each Sales Order form.
5. Select at least these test cases:
   - one full payment in a single journal;
   - one split payment across two journals;
   - one partial deposit with a balance due;
   - one already-returned payment (must be blocked);
   - two customers competing for the last unit in a branch quota;
   - a serial-tracked iPhone delivery;
   - a discount-reason quotation.
6. For successful cases, verify that the original `account.payment` IDs and journals are unchanged, no outbound return is created, no second inbound payment is created, and the final invoice shows the original payment in its reconciled payments.
7. Reconcile the staging accounting entries and compare customer partner ledgers before/after. The pre-order credit should move from outstanding to the delivery invoice with no net cash movement.

## Production gate

Do not install in production until Finance signs off on the partner-ledger result, Retail signs off on serial delivery, and staging confirms that the payment journals have configured outstanding accounts. Any payment on a different receivable/outstanding account is deliberately blocked for Accounting review instead of being silently reclassified.

# Invoice Excel Report

This report is read-only. It never creates payments, reconciliations, or journal
entries.

`Payment` keeps the exact journal name. `Allocation Basis` distinguishes recorded
payments from `Attributed to original payment (report only)`. Attribution applies
only to a posted sales credit with no recorded refund and a posted original invoice
linked by `reversed_entry_id` in the same company and currency. It uses only the
original's recorded financial methods; it never guesses from text or credit offsets.

Partial credits follow the original paid fraction and method mix. All posted sibling
credits and actual refunds consume the same original method capacity, including
siblings outside the export period. Missing or inconsistent evidence is stated in
`Allocation Basis` and remains unattributed.

In invoice currency:

`sum(Payment Amount rows) + Amount Due = Total Net`

`Amount Due` is the report balance. `Accounting Amount Due` preserves Odoo's actual
residual. The on-screen report and Excel export both rebuild from current Odoo data,
so these rules apply automatically to existing and future invoices and credit notes.

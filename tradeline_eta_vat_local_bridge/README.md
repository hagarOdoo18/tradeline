# Existing Apply 14% Tax action guard

This optional auto-install bridge depends on both `accounting_customization` and
`tradeline_eta_reliability`, so its override is loaded after both regardless of
their unrelated dependency depths. It leaves the original accounting module's
Python, server action XML and manifest unchanged.

An approved invoice opens the local-only VAT preview. Confirmed invalid invoices
retain the guarded same-invoice correction, while uncertain requests stay held.
Safe unsent/prepared invoices retain the original accounting action. The bridge
also updates the existing server-action metadata to return its preview modal;
the original source XML remains unchanged. Its post-install tests verify the
resolved registry method and both dispatch paths without financial operations.

For the first deployment, install this bridge explicitly after its two
dependencies are installed: a managed upgrade may discover a new auto-install
addon without activating it automatically. Repeat activation on production.
Before production promotion, verify this module is installed on stage and
`account.move.action_apply_14_tax.__module__` resolves to this bridge.

# Existing Apply 14% Tax action guard

This optional auto-install bridge depends on both `accounting_customization` and
`tradeline_eta_reliability`, so its override is loaded after both regardless of
their unrelated dependency depths. It leaves the original accounting module's
Python, server action XML and manifest unchanged.

An invoice with any ETA history opens the local-only VAT preview or receives its
strict validation message. Only genuinely unsent invoices can enter the original
accounting action. The bridge's post-install tests verify the resolved registry
method and both dispatch paths without touching financial records or calling ETA.

Before production promotion, verify this module is installed on stage and
`account.move.action_apply_14_tax.__module__` resolves to this bridge.

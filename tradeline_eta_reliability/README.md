# Reliable ETA processing

Posted customer invoices and credit notes enter one durable submission queue.
Both the standard EDI dispatcher and the custom Submit button use this queue
when the invoice company has the existing custom ETA credentials configured.
Vendor documents and companies without those credentials retain their native flow.

The worker uses a separate cursor per document, session advisory locks, bounded
network timeouts, and independent commits. Signing does not commit an invoice.
A signed payload and an uncertain-attempt marker are committed before POST.
Known UUIDs are refreshed, never resubmitted. Lost or ambiguous responses are
reconciled against ETA and held when the original payload cannot be identified.
HTTP 429 honors Retry-After. Authentication and signer outages back off. Real
rejections remain visible and require correction; neither invoice dates nor
accounting amounts are changed to force acceptance.

Status checks use the submission API for new submissions and the document API
for older UUIDs whose submission ID was not retained. Due times prevent a bad
document from starving later work. The old two-record status cron is replaced
by a bounded minute worker. Operations Monitor reports unavailable processing,
stalled or uncertain jobs, and rejected documents through its existing setup.

Installing this module retires the two legacy send/status cron schedules. It
auto-installs only when its four dependencies are already installed. Staging
must validate the addon before production deployment. The replacement cron is
initially disabled. Activate it only after setting
`tradeline_eta_reliability.enabled_database` to the exact intended production
database name. A staging copy retains a mismatched name and cannot submit.
Existing uncertain
historical documents must be reconciled before being enrolled for submission.
Invalid documents are not automatically resubmitted, renumbered or cancelled.
After correcting source data, an accounting manager can use the invoice's
existing Submit action. The authority must still report Invalid, and the
document must have changed beyond its date/signature. The rejected document is
retained as a company-owned attachment before a corrected version enters the
durable queue. Accepted documents are refreshed rather than resent.

Validation: run Odoo with `--test-tags=/tradeline_eta_reliability` in an isolated
database. Transport is mocked; tests never submit a taxpayer document.

ETA references:
- https://sdk.invoicing.eta.gov.eg/einvoicingapi/01-submit-documents/
- https://sdk.invoicing.eta.gov.eg/einvoicingapi/09-get-submission/
- https://sdk.invoicing.eta.gov.eg/einvoicingapi/16-search-documents/
- https://sdk.invoicing.eta.gov.eg/integrationpractices/

Operations Monitor > ETA Invoice Log shows the live status of each submission
record, with the invoice link, readable ETA reason, next step, waiting time,
last check and retry time. It defaults to invoices needing attention: held,
invalid or uncertain outcomes, queued/accepted delays of at least 15 minutes,
and approved receipts needing issue-date review. Accepted delays start from
the submission attempt, not the time the job first entered the backlog.
Clear the filter to include approved and newly queued records. ETA incidents
also link directly to affected invoices. Selected-company rules still apply.

This is a current per-invoice status log backed by the existing job ledger;
it is not an append-only history of every state change. Reading it does not
perform HTTP calls, retry documents or alter accounting data. The original
ETA message remains available on each row's detail form. Original invoice dates
stay unchanged; eligible date holds may use the explicitly approved ETA date treatment.

An accounting manager can explicitly approve the current ETA issue date for a
queued or blocked document with no accepted/uncertain government receipt. The
original accounting invoice date and amounts remain unchanged. Approval is
recorded on the job and the prior payload/error is retained on the invoice as
an attachment. JSON and canonical signing use one frozen UTC issue time. A safe
queued retry prepared on an earlier Cairo date is regenerated before POST.
Default processing still uses the original invoice date. This approval cannot
clear an uncertain outcome or overwrite a known ETA receipt.

A manual correction may also regenerate a signature when a fresh ETA response
confirms that the old document is Invalid and every leaf validation error is
4043 (signature digest mismatch). The prior government receipt is archived.
Other unchanged rejections and duplicate documents remain held. Signing and POST
still happen only through the durable worker; date treatment is approved separately.

For EGP lines with no nonzero tax rate, ETA line totals use the existing posted
line amount. This preserves discounts lost by the legacy zero-tax serializer
without modifying invoice dates, accounting amounts or journal entries.

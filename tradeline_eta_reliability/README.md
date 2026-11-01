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
After correcting source data, an invoice user can use the invoice's
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

An invoice user can explicitly approve the current ETA issue date for a
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

Canonical signing reuses Odoo's ETA JSON serializer so quotation marks, backslashes
and control characters retain their JSON escaping. The signing response's CMS
SHA256 message-digest attribute must match those exact canonical UTF-8 bytes
before the worker can send it. ETA remains responsible for certificate and
cryptographic signature validation.

## VAT corrections

Invoice users can use **Correct VAT** on a posted customer invoice and
select the correct company VAT sales tax. Preparing a correction never posts
accounting entries or submits documents to ETA.

- Unsent invoices reopen in draft with VAT applied. Resetting clears stale
  signatures and queued payloads, and archives the previous queue evidence.
- An ETA-invalid invoice can reopen after a fresh authority response confirms
  the rejection. Its full receipt is archived and reposting requires history
  reconciliation before another submission.
- An ETA-valid EGP invoice produces a linked draft credit note matching the
  original authority amounts and a draft replacement containing VAT. The
  credit note references the original ETA UUID. Existing receipt links erased
  by legacy resets are recovered from the retained attachment and verified at
  ETA. If the posted Odoo amounts were changed after submission, a separate
  draft general-journal adjustment preserves the accounting and tax-grid
  balances instead of duplicating VAT. The adjustment uses the original
  accounting date, subject to normal fiscal locks. Original payments remain untouched.

Open **VAT Corrections** to review the drafts. Post the credit note and any
adjustment, wait until the credit note is Valid at ETA, then post the replacement.
The replacement queue also requires a fresh
confirmation that its credit note is Valid at ETA. Normal durable submission,
signing, uncertainty, and duplicate protections apply to both documents.
Repeated preparation opens the existing correction documents.

An unresolved transmission must be reconciled before editing. Foreign-currency
corrections and invoices with earlier posted credit notes require Finance to
review their exchange rates or remaining refundable amounts. Confirmed ETA
cancellations retain the standard draft/cancel flow, with evidence archived;
the cancelled invoice is not automatically queued for resubmission.

Version 18.0.1.0.26 requires a module upgrade for the correction relation,
wizard, access rules, and form views. No existing invoices are corrected
automatically by the upgrade.

## Staff access

The Invoicing group can open invoices, sync and refresh ETA status, retry
corrected rejections, explicitly approve current ETA issue-date treatment, and
prepare VAT corrections without Accounting Administrator or Settings access.
Accounting > ETA Submission Jobs opens the invoice log without exposing the
administrator-only Operations Monitor app. Existing invoice write permissions
and selected-company rules apply before any job action runs. Read-only
accountants may view jobs but cannot change their processing.

Staff have read access to the ETA job and tax/activity/unit reference codes.
Job creation, deletion, direct state edits and private payload access remain
restricted. Validated actions update only the required transport fields after
checking access to every selected invoice. Receipt, uncertainty, retry,
accounting-lock and duplicate safeguards still apply.

Resetting wholly unmatched invoices does not require the branch module's
separate Unreconcile permission. Removing existing payment matches still does.

# Reliable ETA processing

## ETA workspace and VAT policy (18.0.1.0.36)

Today combines invoices dated or created on the current Cairo day with older
invoices submitted or attempted that day. It groups drafts and posted documents
by accounting state and ETA status. All invoices opens grouped by invoice day;
submission day/hour and created day remain available in Group By. Today computes
its date bounds on the server, independently of the viewer's timezone.

Visible historical invoices with missing current VAT are classified immediately
from an identity-matched saved document, without an HTTP call or write while
rendering. Missing or conflicting receipts say Receipt unavailable — reconcile
ETA; staff are not asked to interpret tax fields. The bounded background audit
prioritizes older missing-VAT archives before the remaining approvals from today.

Accounting > ETA Status now provides All invoices, Backlog and active processing,
Sent history, Drafts, VAT issues and queue details. All invoices includes documents
without jobs; Sent history includes earlier native receipts. Search by invoice
number and group by branch, accounting state, invoice day or submission day/hour.
The recorded submission time is the first retained acceptance time, not a log of
every retry. Old receipts without a retained time remain visible under Undefined.

Before any new customer-invoice POST, the final preflight requires positive T1
VAT both in the current tax ledger and in the prepared document. Other taxes,
zero-rate entries and a previously prepared tax-free request do not satisfy this
rule. This applies to the worker, direct send, corrected retries and delegated
native EDI. Read-only receipt reconciliation still runs first, so late approvals
and uncertain earlier submissions are recovered without another POST. Credit
notes preserve the original receipt's taxes and can reverse an untaxed original.

VAT issues distinguishes a missing current tax from a confirmed untaxed receipt.
Review saved VAT reads the matching private archive without HTTP or queue edits.
A separate bounded audit reviews up to 100 saved receipts each minute, prioritizing
missing-VAT records; new full approval responses are audited immediately. Receipt bodies
are evicted from the environment cache after review; upgrades do not scan them. The
original receipt and invoice stay unchanged. Correct VAT prepares reviewable
credit/replacement drafts and waits for ETA approval of the credit. Untaxed
foreign-currency originals are supported only when their posted conversion and
restored credit exactly match the original receipt; unresolved FX differences
remain held for Finance. A correction is marked complete only after approval of
the credit and a receipt-verified VAT-bearing replacement.

The connection banner appears on workspace lists and invoice forms and refreshes
cached readings every 30 seconds. A dedicated once-a-minute job calls only the
authenticated GET /api/v1.0/documenttypes lookup. It observes the exact enabled
database, processing cutoff and ETA retry deadline and never submits documents.
It distinguishes availability, slow responses, access failures, connection
failures, pauses, disabled copies and stale checks. Network send-response and
first-observed approval timings are measured separately; rolling one-hour
averages use invoices visible to the current reader. Approval delay includes
polling time. No samples means Collecting samples, not zero delay. No historical
approval timing is invented and a successful lookup is not a guarantee that a
particular invoice will be accepted.

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


## Accounting staff workflow (18.0.1.0.31)

Accounting > ETA Status provides invoice-number, customer, journal/branch and
original-message searches; status badges and grouping by status, journal,
company and queued day. Invoice-number searches include every status by default. Use Needs attention to focus on exceptions.
Customer-ID holds remain visible even when historical review has no failed
network requests. Each invoice also displays its explanation, next step and
last confirmed check. Reading these screens never makes a network request.

**Send to ETA now** starts a separate invoice-specific operation. It does not
select a queue position, wait for cron activation, or acquire the background
worker's global transport lock. The browser first saves a private five-minute
intent, then executes it in a second RPC. The intent binds database, company,
invoice and requesting user; started/finished intents cannot be replayed.
The existing per-invoice lock and isolated pre-POST uncertainty commit remain
mandatory. One manual operation per company is allowed concurrently with the
backlog. ETA rate-limit deadlines are shared across company transport callers.
Copied databases and owner pauses remain prohibited from sending.

Direct sending can bring forward local connection/signing backoff, but cannot
bypass authority cooldowns, source validation, receipt conflicts or unresolved
transmitted requests. It performs at most three verification/send transitions
for the selected invoice, with a soft 45-second start budget. An in-flight HTTP
request retains its existing bounded timeout. A successful submission means
Received by ETA, not final approval. The durable worker continues recovery and
polling after the direct operation, using the same single submission ledger.

**Check ETA status** uses a GET-only document client and cannot arm a first or
corrected submission. An uncertain attempt is matched to its retained request;
a conflicting/history hold is preserved. Failed status reads do not change the
submission state or automatic retry schedule. Every staff operation retains a
private audit result and a readable invoice chatter entry.

Where the separate Accounting application exists, the ETA menu is placed there
and the unwanted Invoicing app stays hidden, including developer mode and after
an unrelated module re-enables its root. Community-only installations retain
their sole accounting root. Operations Monitor access remains unchanged.

**Correct VAT** previews the selected tax's price-included or price-added
treatment, new totals and change to the customer total before creating drafts.
Preparing corrections still does not post accounting entries or send to ETA;
the linked credit must be approved before the replacement can be posted.

Release validation includes the complete ETA suite plus staff access, busy
backlog transport, same-invoice locking, GET-only checking, uncertainty,
authority deadlines, saved-intent rollback/replay and menu-visibility tests.
Test transport is mocked; synthetic tax documents are never sent to ETA.

# Reliable ETA processing

Tax headers combine posted journal items of the same ETA tax type into one
total. Separate invoice lines keep their own rates and tax subtypes. Repeated
tax types on a single item are held locally for Finance correction, preserving
the posted ledger and preventing another CF316 rejection. Formatting and
validation never change accounting entries or use a second sender.

## Daily operations alerts (6 November 2026)

ETA document delay, rejection and no-progress alerts cover invoices created
and dated on or after a fixed Cairo start date. The parameter
`tradeline_eta_reliability.daily_alert_start_date` is initialized to the Cairo
date of the first monitor check after this code release; production rollout
sets it explicitly to `2026-11-06`. It does not advance each day, so unresolved
new invoices continue to alert. Both the original invoice creation timestamp
and invoice date must meet this boundary. Re-enrolling, retrying, re-dating the
ETA request or refreshing an old job does not bring backlog into daily alerts.
Historical flags do not exclude new invoices that later enter recovery.

The old all-history aggregate incidents retire silently, without backlog
recovery emails. New incidents use `eta:daily_*` codes and open the matching
daily invoice cohort. The full ETA log and attention count retain all history;
backlog processing, corrections, reconciliation and transport safeguards are
unchanged. Scheduler/configuration failures still alert. This is a code-only
release with no schema or installed-version change.

## Invoice QR codes on reprints (18.0.1.0.55)

Invoice downloads and customer-email attachments check their cached PDF against
the Ahmed Salama receipt's QR URL. A PDF made before the receipt, or containing
an older share key, is regenerated locally on the next download. This also repairs
legacy cached PDFs when they are first downloaded after this upgrade. Previous
files remain attached to the invoice for history; already downloaded files and
old email attachments are unchanged. Staff should use the invoice's current
Print/Download action to obtain a fresh copy.

Receipt-link changes release stale PDF caches immediately. PDF generation holds
the invoice row while rendering and records the rendered QR URL, preventing a
late pre-receipt print from replacing a newer cache. This uses the configured
invoice report only: no sending wizard, ETA submission, signer or status request
runs during a download. A receipt UUID without an authority share key cannot
produce an invented QR link. ETA's anonymous share links are temporary; a portal
expiry error is separate from invoice validation and cannot be cured by sending
another copy of an approved invoice.

## Automatic recovery for new recoverable HTTP 422 failures (18.0.1.0.50)

New document-POST failures with an observed HTTP 422 and either no structured
error code or DuplicateSubmission carry a private versioned recovery policy, bound to the exact durable
attempt. After at least 605 seconds from the **observed response**, or a longer
retained delay, the normal worker runs the same complete-history, fresh receipt,
source, tax, signing and final preflight checks as the manual recovery below.
Unchanged requests retain their signed bytes; the existing narrowly verified
business street/city correction can be prepared and signed again. No invoice
date, amount or tax is edited. Old failure records are not silently opted in.

Each invoice can queue at most three automatic HTTP 422 recoveries. A persistent
rejection then displays a review reason and stops further automatic POSTs;
GET-only checks still restore a late receipt. Missing proof, conflicting receipts,
changed fiscal facts and transport timeouts never authorize a retry. **Check ETA
status** remains GET-only even for new failures. Invoice users see that recovery
is scheduled; a negative status check preserves the existing due time, reason
and failure count so repeated checks cannot keep postponing recovery. A matching
receipt still updates the job normally. Invoice users also see
its next retry time and any deferral or review reason. Ordinary
cooldowns with a healthy enabled worker are not labeled as needing correction.
Queued retry history now retains the number of earlier attempts rather than
claiming that no attempt has been recorded.

## Manual recovery of unclassified HTTP 422 (18.0.1.0.49)

**Send to ETA now** can reconcile an observed HTTP 422 whose structured error
code was missing. It requires the worker's private diagnostic to match the exact
database, company, attempt, time and signed-payload hash. It waits at least ten
minutes and five seconds, honors a longer retained Retry-After, then requires
complete taxpayer-scoped history plus a fresh fully paginated receipt search.
A matching receipt is restored; conflicting receipts, changed source, missing
proof, timeouts and other errors remain held. Only verified absence allows the
retained signed bytes into the normal final preflight and durable POST flow.
If only the street/city changed for the same named domestic business with the
same nine-digit tax ID, the old request is archived and the current address is
prepared and signed under the same transaction. All other source fields must
still match, and the final preflight checks the newly signed source again.
Invoice date, posted totals, taxes and the original attempt archive are preserved.
Cron and **Check ETA status** do not authorize this recovery. Future failures also
retain the bounded Retry-After, and the client recognizes structured root-level
error codes without guessing a code from the HTTP status or message text.

## Explicit exemptions and manual VAT bypass (18.0.1.0.47)

ETA signing and transport belong to Ahmed Salama's `egyptian_electronic_invoice`
connector, extended by this addon. Odoo's native ETA HTTP transport and thumb-drive
signing entry point are disabled; its installed model dependency retains earlier
receipts and duplicate-prevention evidence. The shared Accounting engine remains
required by Ahmed's module. No Odoo core files are modified.

Invoices whose product lines explicitly carry zero-rate T1 subtypes V001–V008
can be sent at 0% through Sync. The outgoing line classifications must match the
recorded source. A general-sales V009 tax at zero, an empty tax selection, and an
exemption on only one of several unclassified lines do not qualify automatically.

Central accounting can choose **Send without VAT…** on the invoice or its ETA
queue detail, enter a reason, then **Approve and send without VAT**. This records
the user, time, reason and source fingerprint in a private audit and enters the
existing durable send flow. The approval only bypasses the local positive-VAT
requirement for that unchanged invoice. Edits invalidate it; copies do not inherit
it. Receipt reconciliation, uncertain-delivery holds, ETA validation and cooldowns
still apply. Existing held jobs are not automatically requeued by this release.

The original ETA reason and next step are visible above the invoice. An earlier
VAT hold on an explicitly exempt invoice explains that Sync can now retry it.


## ETA workspace and VAT policy (18.0.1.0.38)

Find any invoice searches all allowed dates/statuses, including invoice number,
branch reference, a pasted full display label, and an Odoo invoice link. A unique
match opens the invoice directly; page-specific domains never hide the result.
Approved invoices provides the explicit success log grouped by submission day.

An authenticated complete receipt search can classify a retained unknown request
as ETA receipt not found — delivery review. Its private proof binds the database,
invoice, actual signed-payload SHA, attempt marker and search window. The worker's
unknown state and duplicate-prevention rules do not change. Staff see the dated
search result and need for ETA delivery confirmation. Ordinary RPC writes cannot
forge the proof. A schema initialization copies only existing state values into
the staff grouping column, avoiding a signed-payload recomputation on upgrade.

Credit drafts preserve configured zero-rate tax codes from the authority receipt.
Foreign-currency alignment drafts carry the original currencies and exact offsets
for both company balances and currency amounts. Receivable differences, changed
receipt FX rates, currency-only residuals and inconsistent signs remain blocked
for Finance. No correction document is posted or submitted during preparation.

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
VAT both in the current tax ledger and in the prepared document, unless all lines
carry matching explicit zero-rate classifications or central accounting has
recorded a current invoice-scoped VAT bypass. Other taxes,
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
The native ETA HTTP transport is disabled; missing Ahmed connector credentials
produce a configuration hold rather than falling back to Odoo's sender.

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
private audit result and an immediate persistent response to the requesting user. ETA technical notes are not published to branch invoice followers.

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


Release 18.0.1.0.39 separates VAT work into never-submitted invoices (correct the
same invoice), verified ETA-approved zero-VAT receipts (credit and replacement),
existing correction documents, confirmed ETA Invalid, and delivery/receipt review.
Opening a work list never creates or posts documents. Existing corrections open
active documents by default; History includes cancelled and superseded drafts.
Instructions recognize an already-approved credit and prevent duplicate preparation.

ETA Status uses the dedicated central accounting group, without granting posting
or company access. Menus, bookmarked actions, workspace views, health readings and
recovery entry points enforce that role. Branch users retain normal financial invoice controls; ETA controls, fields, technical pages, ribbons and status summaries require explicit central membership, even in developer mode. Membership is maintained separately from upgrades.
The connection component remains in the ETA workspace; the native invoice form
has no dependency on its widget registration, avoiding stale-asset form crashes.
The separate accounting_customization Apply 14% Tax implementation is unchanged.

Release 18.0.1.0.40 groups confirmed ETA intake refusals and final Invalid
receipts together as ETA rejected — needs correction. Local checks, generic
HTTP failures, source/receipt conflicts and unresolved sends retain their
separate labels. The upgrade refreshes only presentation labels in bounded
batches; it does not reset transport states, retries, timestamps or invoices.

Release 18.0.1.0.41 keeps the familiar customer invoice list and its saved
filters/grouping. ETA status is visible beside the invoice number, with row
Sync buttons using the existing direct-send operation. Optional
issue and next-step columns are available without opening the ETA workspace.

ETA-only native EDI processing notices and the duplicate native summary are
hidden from the familiar invoice form; central accounting retains receipt-backed status in the old list and ETA workspace. Cached native Process
now/Retry buttons route through the same durable staff operation; accepted and
uncertain invoices receive status checks rather than another submission. Other
EDI formats keep their controls. Ahmed Salama's preparation module, the queue,
background processing, native receipt fields/archives and invoice header remain
installed and unchanged. No module uninstallation or invoice migration occurs.

The familiar invoice form has a single Sync button for direct sending. Its
original E-Invoice Details / Update Status control remains GET-only. The added
Correct VAT header button, duplicate status-check header and added ETA summary
panel are removed from the visible invoice form; dedicated ETA work lists
remain available to central accounting. Posting state and normal invoice
controls are unchanged.


The .41 release supersedes the earlier approved-invoice correction policy.
As of .46, **Actions > Apply 14% Tax** applies an Odoo-only amendment directly
to all selected eligible ETA-approved invoices, including selection across pages.
The ETA-approved VAT work list offers **Add 14% VAT in Odoo** on each row with
the same direct behavior. Neither action opens a preview. The amendment changes eligible product
lines to the existing 14% company VAT while preserving the gross total,
receivable lines, residuals, payment reconciliations and original ETA evidence.
It creates no credit note, replacement, queue request or government operation.
Mosad's original accounting_customization files remain unchanged; invoices
without authority history continue to use that original tax action.

The local path is blocked for closed periods, hashed/audit-protected entries,
cash-basis or analytic allocations, unresolved/conflicting ETA outcomes,
existing credit/correction documents, and tax choices that change customer
gross totals. Each amendment locks and revalidates its invoice fingerprint;
concurrent changes fail atomically. Each selected invoice has its own savepoint,
so blocked records are reported without undoing successful eligible records.
Mixed selections with authority history never enter the original reset/post path.
Private immutable evidence records
before/after tax and preservation hashes. Completed local corrections display
VAT added in Odoo — ETA unchanged and leave the actionable VAT lists. Historical
credit/replacement documents remain separately available for review; cached old
public correction wizards cannot create new credits for submitted invoices.

Confirmed historical rejections now use the needs-correction heading. This
changes presentation only: duplicate/uncertainty holds and all actual transport
state, retry timestamps and receipt evidence remain intact. The bounded .41
migration refreshes staff labels without re-enrolling or replaying the backlog.


Release 18.0.1.0.45 keeps confirmed ETA-approved zero-VAT invoices in the
Odoo-only VAT list even when the native Egyptian EDI mirror says to_send or
to_cancel. The local amendment preserves those native markers and all receipt,
job and submission evidence byte-for-byte; it never processes those documents.
Actual unresolved job attempts, duplicate/receipt conflicts, other EDI work,
accounting locks and existing credit/correction documents keep their guards.
Completed local amendments remain completed despite unchanged native ETA flags.

Release 18.0.1.0.46 removes the added ETA summary panel and its detail/review
buttons structurally from the familiar invoice form. Status remains in the
old invoice list and dedicated workspace; Sync and the original Update Status
location are retained. No hidden copy of the summary remains in form metadata.

Release 18.0.1.0.47 processes a selected VAT list immediately in sequential
requests of at most 25 invoices, with progress and a final applied/skipped/blocked
result. No preview is required, including Select all across pages. An interrupted
connection reports only acknowledged results and refreshes the work list; invoices
already carrying VAT are skipped on retry. Each bounded request retains ordinary
transaction handling and per-invoice savepoints, without explicit commits or ETA jobs.

The code-only deployment keeps installed schema versions (.45 on stage, .44 on
production; bridge .1 and product 0.1). The reviewed fields, ACLs, dependencies
and asset paths did not change. Git revisions and runtime capabilities identify
this VAT release. Existing summary/product view changes were applied separately;
the workspace's approved-invoice row button is updated after the new methods load.

Release 18.0.1.0.48 adds internal ETA event notes to the invoice chatter and
the same history on the ETA status detail screen. It records new attempts,
receipt/approval/rejection results, changed hold reasons, approved issue-date
treatment and VAT bypass approval. Notes include the explanation, ETA error
codes, next step and scheduled check time in Cairo. Identical background checks
do not add notes. Receipt acceptance is distinct from final ETA approval.

The notes are created directly with an internal message subtype and no
recipients or notification pipeline; they do not email followers or customers.
History readers must pass the source invoice's access checks. Existing jobs
record their retained status before the first subsequent event; this is labeled
as a snapshot rather than reconstructed history. The upgrade does not backfill
or replay invoices, change transport policy, or submit documents.

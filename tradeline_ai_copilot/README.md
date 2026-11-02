# Tradeline AI BI Copilot — executive chat

Odoo 18 add-on. Open **Tradeline AI → Chat Workspace**, or use the floating chat
button from another Odoo screen. This upgrades the existing Copilot; the separate
`tradeline_ai_invoice_chat` module is not a dependency and is not changed.

## What changed

- Claude and OpenAI can discover allowed models, inspect readable fields, and
  execute validated queries in a bounded multi-step conversation.
- Record previews, counts, sums, averages, distinct counts and date groupings use
  the Odoo ORM as the requesting user.
- Bar, line and doughnut charts use Odoo's bundled Chart.js. The chart and its
  supporting table share the same server result. Raw record IDs are never chosen
  as chart measures. Counts, refunds/negative values and zero shares are handled.
- Searchable personal chat history, follow-up context, suggested questions,
  responsive conversation drawer, floating/fullscreen modes, and error recovery.
- Replies stream from Claude/OpenAI as text arrives, with a Stop control and
  an explicit unverified notice if a response is interrupted. Tables and charts
  appear from the completed query result; prose is displayed as paragraphs.
- Excel/CSV exports preserve the query's grouping and measures, recheck current
  access, and refresh the data. A limited result is identified as limited.
- Each completed data result includes **Sources & filters**: model, execution
  time (UTC), active companies, actual filters, grouping, measures and row limit.
  Source IDs such as `[S1]` are assigned by the server and passed to the model.
  Missing/invalid citations and answers without completed queries get a warning.
  Source metadata documents the query; it does not certify the model's prose.
- Material ambiguity uses a dedicated `request_clarification` response. It ends
  the tool round before bundled data calls, offers optional editable reply
  choices, and does not display the missing-data warning. Unsupported business
  answers without a completed query still receive that warning.
- Copy-answer and jump-to-latest controls, compact supporting tables (over 12
  rows start collapsed), clearer analysis preview notices, and Tradeline brand
  artwork with a native SVG AI accent keep long conversations manageable.

## Architecture

```mermaid
flowchart LR
    UI[Odoo chat workspace / popup] <--> S[Authenticated Copilot service]
    S <--> AI[OpenAI Sol or Claude\nplanning and streamed explanation]
    S --> C[Clarification question\noptional editable choices]
    C --> UI
    S --> T[Validated read-only tools]
    T --> P[Model / field policy\nuser ACLs and company / branch rules]
    P --> D[Saved Odoo, legacy and Shopify-linked records]
    T --> R[Deterministic aggregates\ncohorts, identity resolution, basket math]
    T --> M[Versioned invoice revenue / ledger gross margin\nsource freshness watermarks]
    T --> L[Administrator-only live Shopify\nfixed connection / order GraphQL reads]
    R --> S
    S --> W[Separate OpenAI public web search\npublic subject only, cited URLs]
    W --> S
    S --> H[Private chat history, audit and report files]
    S --> UI
```

The service has its own streaming cursor; it does not receive unsaved form data.
The selected language model receives the question, bounded recent conversation
and bounded tool results. Raw identifiers used by the identity resolver remain
inside that server-side helper. Public research is a separate request without
automatic Odoo-record or conversation attachment. API keys stay on the server.
The only writes belong to Copilot's history, audit and generated report storage.
There is no business-operation tool. Downloads rerun queries under current access
checks, including fresh live reads for Shopify reports.

Versioned financial definitions, saved-data watermarks and bounded live Shopify
order reporting are implemented. These definitions are explicit reporting bases,
not a claim of finance-team approval or certified account classification. Product
scope follows the user's prompt and confirmed follow-ups; example exclusions do
not become global filters. Model routing should follow measured accuracy, cost
and latency on that benchmark rather than an untested complexity heuristic.

## Configuration and upgrade

Upgrade **only `tradeline_ai_copilot`** on the intended Odoo instance, restart its
Odoo process as required, and refresh the browser assets. Existing conversations,
API keys, provider selection and model-access policies are retained. This change
does not install React, Tailwind, npm packages, a database extension, or another
Odoo application.

The streaming endpoint uses authenticated, CSRF-protected POST requests. It owns
its own database cursor and commits completed chat messages independently of the
open ERP form. Reverse proxies must permit streaming and disable response
buffering for `/ai_copilot/chat/stream`; the endpoint sends `X-Accel-Buffering: no`.
Keep its upstream timeout longer than the configured reporting time budget.

In **Tradeline AI → Administration → Settings**:

1. Select the default provider and set its API key using the administrator form.
2. Select a supported tool-capable model ID. The default for new Claude settings
   is `claude-sonnet-5-5`; existing configured model IDs are preserved and may need
   updating if retired. Check [Anthropic's current models](https://platform.claude.com/docs/en/models/overview).
3. Enable **Flexible Queries**, charts, and the required export formats.
4. Review **Model Access Policy**. Disabled models stay disabled when refreshing
   the catalog. Field allowlists, aggregation permission and export permission
   are enforced independently of Odoo's own permissions.
5. Grant **Tradeline AI / Reporting User** only to approved internal users in
   **Settings → Users & Companies → Users → Access Rights → Tradeline AI**.
   Alternatively, use developer mode **Settings → Users & Companies → Groups**,
   open **Reporting User** under Tradeline AI, and add the selected users.
   Users should refresh their browser after a membership change.

The built-in **Administrator** account receives reporting access automatically.
Other accounts, including other administrators, are opt-in: upgrading does not
automatically enrol them. Nonmembers do not see the popup or reporting menus and cannot
call chat/query/export APIs or access Copilot conversation/report models.
Administrators retain configuration, policy and audit management; they need the
Reporting User group as well to use the chat. Group membership never grants
access to additional companies, branches or underlying business records.

Both providers are supported. Enter an **OpenAI API Key** and a compatible
tool-capable **Default OpenAI Model**, then choose **OpenAI** as the default
provider or in the chat picker. Claude credentials can remain configured at the
same time. Credentials stay in the administrator-only settings on the server.

The provider picker uses these configured models, rather than obsolete hardcoded
model choices. An API key is needed for a live AI response; missing credentials
and failed connections are reported explicitly. The application never substitutes
a keyword guess and presents it as a verified flexible answer. The old query
engine remains available by disabling Flexible Queries, with a visible warning.

## Data boundaries

The read-only tools are `search_models`, `describe_model`, `query_data`,
`business_context`, `compare_customer_sets`, `analyze_baskets`, `metric_definitions`,
`financial_report`, `data_freshness`, and (eligible administrators) `live_shopify`.
`research_public_web` is available when an OpenAI key is configured and the
`tradeline_ai_copilot.enable_web_search` system parameter is `True` (default).
Set it to `False` to disable public research. Web searches incur provider usage.

### Financial definitions and freshness

`invoice_net_revenue` sums posted customer invoices and credit notes once at the
header level, using signed untaxed amounts in company currency by invoice date.
Discounts are included and refunds reduce the result. It does not add sales
orders, POS, legacy or Shopify figures, and is separate from GL revenue.

`ledger_gross_margin` uses posted journal items by accounting date: revenue is
minus the balance of `income` accounts; COGS is the balance of
`expense_direct_cost` accounts. Gross profit = revenue minus COGS. Margin % uses
gross profit / positive revenue. With no visible direct-cost entries, profit and
margin are unavailable, never assumed to be 100%. Other income, overhead,
depreciation and tax accounts are outside this definition; this is not net profit.
The chart of accounts and completeness of cost postings still need review. No
current product cost, estimated invoice cost, fixed tax divisor or new FX rate
is substituted. Company/currency buckets remain separate. Missing financial
field/model permissions fail closed. Source definitions and freshness survive
Excel exports; exports refresh their evidence.

Source panels distinguish execution time from data freshness. `data_freshness`
reports visible saved modification and selected business-date watermarks. Neither
write_date nor latest order date proves a successful complete upstream sync.
No freshness SLA or automatic stale/fresh classification is invented.

### Live Shopify boundary

Live connection, order summaries and order lists use fixed GraphQL query strings,
existing connector tokens and the instance's stable API version. Only exact
`*.myshopify.com` hosts are accepted; redirects are disabled, response size and
time are bounded, and API bodies / credentials are omitted from errors. No
credential refresh, imports, sync routines, webhooks or mutations are invoked.
An expired token must be refreshed through the normal connector workflow.

Access requires existing Odoo administrator membership, Reporting User access,
instance model/field policy, instance record rules and active-company ownership.
No groups or Shopify scopes are expanded. Live orders are **store-wide**, not
subject to Odoo per-order branch rules, which is why ordinary reporting users
retain saved-record reporting only. Credential fields (including legacy connector
aliases) are unavailable to AI discovery/query/filter/export.

Dates mean order creation dates in Shopify's store timezone. Complete reads are
bounded to 2,000 orders / 20 pages / 25 seconds; interrupted or incomplete reads
discard totals. Older windows require existing `read_all_orders` access, otherwise
the report is refused. Pagination is not a frozen transactional snapshot.
Financial/fulfillment/cancelled/test filters follow the prompt and are applied
deterministically after the complete date population is read. No global product
or status exclusions are introduced.

Current order value includes Shopify's discounts/taxes and current return/edit
state; it is **not** recognized revenue, Analytics net sales, cash receipts or
profit. Lifetime refunds are separate and must not be subtracted again. Live
product/inventory/customer-identity APIs are not part of this first bounded
surface; saved synced models remain available under Odoo policies.

References: [Odoo reporting](https://www.odoo.com/documentation/18.0/applications/finance/accounting/reporting.html),
[Shopify order semantics](https://shopify.dev/docs/api/admin-graphql/latest/objects/Order),
[Shopify order access scopes](https://shopify.dev/docs/api/usage/access-scopes),
[Shopify search validation](https://shopify.dev/docs/api/usage/search-syntax).

Customer cohorts compute full unions/differences before applying preview limits.
Keys must be saved customer relationships, not matching numbers from separate
databases. Legacy invoice lines can resolve missing customer IDs through existing
strict/linked, unambiguous invoice bridges. Other links are excluded and counted
as unresolved rows. Optional `resolve_legacy_identifiers=true` also compares
header/line phones and national IDs against current contacts without saving links.
Egyptian mobile formats and explicitly international numbers are normalized;
individual VAT values can represent structurally valid Egyptian national IDs.
Unique exact identifiers only: shared numbers, duplicate IDs and conflicting
signals stay unresolved. These are reporting matches, not authenticated identities.
Raw identifiers stay server-side and are not returned to the AI or source metadata.
The lookup includes archived contacts, applies current-user permissions, is reused
within a cohort request and refuses partial indexes above 200,000 visible contacts
or the 25-second analysis budget. Uniqueness is within readable contacts only.
If exclusions contain unresolved identities, candidates are
provisional. Duplicate contacts are not merged and returns are not automatically
netted. Filter actual handset products/categories to exclude accessory mentions.

Basket analysis deduplicates products per document and reports support,
directional confidence and lift. It is evidence of association, not causation or
profitability. Both analyses enforce source/linked-record permissions, aggregation
policies and field restrictions, use ORM reads, and fail above 50,000 grouped
keys or their time budget. Exports rerun the same analysis with current access;
metadata includes every source definition and its coverage limitations.

Public research uses a separate, nonstored OpenAI Responses request with hosted
web search. It receives a short public subject and no automatic attachment of
Odoo records or chat history. Obvious private identifiers are rejected; the
reporting agent is instructed not to place private business data in subjects.
Public evidence uses clickable `[W1]` citations; Odoo query evidence uses `[S1]`.
Web content is treated as untrusted data. Responses are bounded and require real
HTTPS citation annotations before they are accepted.
There is no write, create, delete, workflow, arbitrary Python or model-written SQL
tool. Business models are queried without `sudo`, with the active user's ACLs,
record rules and active-company context. Existing branch restrictions continue to
apply through those rules. Company access does not become broader for executives.

The add-on writes its own conversations, messages, audit records, configuration
catalog and generated report attachments. It does not post invoices, alter stock,
confirm orders or change business records. Metadata-policy and audit operations
use narrowly scoped administrator access; business queries do not.

Reports use saved records only. Unsaved invoice/order fields are never sent to
the chat endpoint. The popup does not invoke form Save, Discard, navigation or
workflow actions. Its keyboard events stay within the popup, closing restores
the prior field focus, and background response completion cannot take focus from
an ERP field. Configuration/history load when the popup is opened, rather than
adding chat requests to every backend page load. Closing the popup allows the
report to finish in the background; Stop cancels the response.

Technical models, Copilot credentials/history models, user credential records,
binary/HTML fields and secret-like fields are blocked. Field policies also apply
to filters, sorting, grouping and relationship traversal, not just output columns.
Only direct safe fields are returned; filters can traverse up to two many-to-one
relations, validating the related model and field access at each step.
Abstract services and Odoo's virtual UI helper models are excluded. A missing
custom selection callback excludes that field from reporting, so other readable
fields on the model remain available. The add-on does not modify the custom module.

Discovery is not limited to Sales/Accounting/Inventory: it uses the registry of
installed models and the administrator's policy. Search by model label, technical
name or module name, and continue in pages of 60 using `offset`. Uninstalled
applications, wizards, sensitive models and models denied by ACL/policy are not
available datasets. Reporting-group membership does not bypass these restrictions.

Queries allow up to 500 returned records/groups (also bounded by model policy),
three group dimensions, twelve fields per section, and eighty filter terms.
Each database read has an 8-second statement timeout that is restored afterwards.
Investigations default to six provider rounds, at most eight data queries, and a
total time budget capped at 90 seconds. Long text cells are limited to 2,000
characters and identified as shortened. Exports also obey format and model limits.

Monetary aggregates across multiple currencies require currency grouping. The
assistant is instructed to distinguish sales orders, invoices, POS and payments,
explain dates/refunds/taxes, and avoid treating a limited preview as a total.
Business-specific measures still need validation against the company's reports.

Questions, bounded tool results and recent conversational text are sent to the
selected provider. API keys stay server-side. Provider error bodies and keys are
not logged. Files and model output are escaped in the chat, and spreadsheet text
cannot become a formula or hyperlink merely because of its contents.

## Interface implementation

The shadcn-inspired interface is implemented in Odoo OWL, using scoped `tl-ai-*`
styles. It is not a React/shadcn application embedded in the ERP. White surfaces,
slate text, a pale conversation rail, black primary controls, blue chart accents,
1px borders and 7–12px radii form the local design system. The main chat is capped
at 860px; tables scroll within results; the composer stays visible. Focus rings,
labelled controls, live progress/error messages, RTL message direction, reduced
motion, and an accessible data table accompany charts.

## Validation

Product quantity questions resolve the actual catalogue first with
`search_products`, including archived variants and delegated stored template
fields (names, categories and units). Ordinary product-unit questions default to
all available saved records when no period is supplied; genuinely competing
catalogue interpretations still require one concise clarification.

`product_sales_units` uses posted customer invoice quantities minus credit notes,
plus completed POS orders without an invoice link. Quantities are converted to a
common readable unit category. Imported legacy totals stay separate; visible
archive dates and unmapped description candidates are reported, and missing
sources are unavailable rather than zero. No lifetime completeness or migration
deduplication is implied. The same definition is rechecked for exports. Sales
orders and Shopify order values are not added to this unit measure.

Date/datetime minimum and maximum aggregates are supported; sums/averages of
dates remain invalid. A successfully repeated identical tool request clears its
transient warning. Success on an unrelated scope cannot clear an unresolved
source failure.

The Odoo tests in `tests/` exercise permission and company boundaries, unsafe
queries, field-policy enforcement, grouped totals, limits, grouped Excel export,
spreadsheet formulas, provider protocols/failures, follow-up context, and chat
ownership. Run with an isolated database:

```powershell
python odoo-bin -c <test-config> -d <isolated-test-db> -i tradeline_ai_copilot --without-demo=all --test-enable --test-tags /tradeline_ai_copilot --stop-after-init --max-cron-threads=0
```

Browser verification should cover saved chat → source table/chart → Excel download,
new chat → suggested prompt → answer/error → reload/history, and the mobile drawer.
Mocked-provider tests prove the integration protocol, not live model accuracy.
Validate real Claude answers against established Odoo reports in the intended
company/branch context before executive use.

Streaming regression tests also cover incremental UTF-8 text, fragmented tool
arguments, provider completion/error handling and final message persistence.
For form compatibility, test both existing drafts with unsaved changes and new
unsaved invoices/orders: open/resize/close, multiline input and form shortcuts,
stream while typing in the underlying field, complete while closed, Stop, chart
rendering and Excel download. Verify unchanged business record counts, values,
states, lines and write dates, and no business `web_save`/write/workflow requests.

## App menu visibility

The app and all its submenus are visible only to the built-in Administrator
account (`base.user_admin`), including in developer mode. Other Settings
administrators do not see the app. Existing model/API permissions and scheduled
operations are unchanged.

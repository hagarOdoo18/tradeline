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

Only three tools exist: `search_models`, `describe_model`, and `query_data`.
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

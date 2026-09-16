# Tradeline AI Assistant  (`tradeline_ai_invoice_chat`)

Chat assistant for Odoo 18 that answers natural-language questions (Arabic or
English) about **any data in the system** — not only invoices.

> The technical name of the module is unchanged (`tradeline_ai_invoice_chat`),
> so an already-installed database only needs an **Upgrade**, no migration.

## What changed (v18.0.2.0.0)

| Before | Now |
|---|---|
| Keyword → hardcoded SQL, invoices only | Hybrid: 13 curated reports **+** a generic, schema-aware query layer |
| AI only rephrased a fixed data block | AI calls audited **tools** and decides what to read |
| Accounting users only | Any internal user; data filtered by their own rights |

## Architecture

```
chat_widget.js  ──►  /ai_invoice_chat/send  ──►  controllers/main.py
                                                     │
                                       OpenAI-compatible tool loop
                                                     │
        ┌────────────────────────┬───────────────────┴────────────┐
        ▼                        ▼                                ▼
 ai.data.schema           ai.data.query.engine            invoice.query.engine
 (what may be read)   (validated search_read/read_group)   (13 pinned reports)
```

### AI tools

| Tool | Purpose |
|---|---|
| `search_models(keyword)` | find the technical model holding the data |
| `describe_model(model)` | readable fields, types, relations, selections |
| `query_data(model, domain, fields, group_by, aggregates, order, limit)` | validated ORM read |
| `run_report(report, date_from, date_to, limit)` | curated pinned report |

### Safety

* **No raw SQL from the AI.** Only `search_read` / `read_group`.
* Every domain leaf, field, group-by and aggregate is validated against
  `fields_get()` before execution; anything unknown is rejected.
* All queries run as the **current user** — ACLs, record rules, multi-company
  and branch restrictions apply automatically.
* Hard-blocked whatever the settings: `ir.*`, `bus.*`, `iap.*`, API keys,
  passwords, tokens, signatures, binary/image fields.
* Row/group ceiling per query (default 100, hard max 500).

## Pinned reports (`run_report`)

`invoice_summary`, `invoice_by_type`, `overdue_invoices`,
`payments_by_branch_journal`, `paid_by_branch`, `invoice_date_range`,
`top_customers`, `sales_orders_summary`, `top_products`, `stock_on_hand`,
`purchase_summary`, `pos_summary`, `hr_headcount`.

Reports for modules that are not installed degrade gracefully with a clear
message instead of raising.

## Configuration

**Settings → General Settings → Tradeline AI Assistant**

| Setting | Config parameter | Default |
|---|---|---|
| API Key | `ai_invoice_chat.openai_key` | *(empty)* |
| Model | `ai_invoice_chat.openai_model` | `gpt-4o-mini` |
| API Base URL | `ai_invoice_chat.base_url` | `https://api.openai.com/v1` |
| Max Rows per Query | `ai_invoice_chat.max_rows` | `100` |
| Allow Any Model | `ai_invoice_chat.allow_all_models` | `False` |
| Additional Models | `ai_invoice_chat.extra_models` | *(empty)* |

The model **must support tool/function calling** (gpt-4o-mini, gpt-4o,
deepseek-chat, llama-3.x-70b-tools ...). Without a key the module falls back to
Odoo IAP, then to formatted raw reports from the keyword router.

## Sample questions

* "أعلى 10 عملاء من حيث المبيعات هذا العام"
* "Stock on hand for products in category Electronics"
* "POS sales by session yesterday, per payment method"
* "Which purchase orders are still waiting for a bill?"
* "Headcount by department"
* "Compare invoiced vs paid per branch for last month"

## Friendly & Arabic (v18.0.3.0.0)

**Tone.** The system prompt now asks for a warm, colleague-like voice: headline
answer first, compact markdown tables for breakdowns, one noticed insight, one
concrete follow-up question, and a quiet italic source line. Raw tool output,
model names and error text never reach the user — failures are apologised for in
half a sentence with an alternative offered.

**Arabic.**

* Replies mirror the question's language (Egyptian dialect welcome).
* `_extract_dates()` understands: اليوم/النهاردة، أمس/امبارح، هذا الأسبوع،
  الأسبوع الماضي، هذا الشهر/الشهر ده، الشهر اللي فات، الربع الحالي/الماضي،
  الربع الثاني 2024، هذه السنة، السنة الماضية، آخر ٣ شهور، اخر 30 يوم، أسماء
  الشهور (يناير…ديسمبر) — plus Arabic-Indic digits ٠١٢٣٤٥٦٧٨٩ and harakat/tatweel
  stripping.
* Message bubbles auto-detect direction (RTL for Arabic) while numeric tables and
  fixed-width report rows stay LTR so figures never scramble.
* `i18n/ar.po` translates menus, settings, and the chat UI strings.

**Chat UI.**

* Markdown tables render as real HTML tables (numeric columns right-aligned,
  zebra rows, horizontal scroll) instead of monospace text.
* Grouped suggestion cards on the welcome screen, "Quick asks" chip row, per-message
  timestamps, copy-answer button, friendlier empty and loading states.
* All rendering is HTML-escaped before formatting (no injection from model output).

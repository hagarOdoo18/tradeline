# -*- coding: utf-8 -*-
"""
AI Assistant controller
=======================
Turns a natural-language question into answers over the WHOLE Odoo
database.  The AI never touches the database directly: it may only call
nine audited tools, all executed with the current user's access rights.

    search_models(keyword)                  -> find the right model
    describe_model(model)                   -> list its readable fields
    query_data(model, domain, fields, ...)  -> search_read / read_group
    multi_query(queries)                    -> run multiple queries at once
    run_report(report, date_from, date_to)  -> curated pinned reports
    get_field_values(model, field, ...)      -> distinct values of a field
    count_records(model, domain)            -> fast record count
    search_by_name(model, name, ...)        -> find records by name/ref
    generate_excel(title, columns, rows)    -> create downloadable Excel file

Fallback chain: OpenAI-compatible API (tool calling) -> Odoo IAP ->
formatted raw data from the keyword-routed pinned reports.
"""
import base64
import io
import json
import logging
import os
import urllib.request
import urllib.error
from datetime import date

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 12
MAX_TOOL_OUTPUT = 15000
DEFAULT_BASE_URL = 'https://openrouter.ai/api/v1'
DEFAULT_MODEL = 'openai/gpt-4o-mini'

_SYSTEM_PROMPT = """\
You are the Tradeline AI Assistant -- a friendly, expert colleague inside an
Odoo 18 ERP. You can answer about ANY part of the system: sales, invoicing, payments,
purchases, inventory, point of sale, products, customers, vendors, employees, payroll,
projects, manufacturing, maintenance, fleet, approvals, quality, and more.

WHAT YOU CAN DO
- Answer data questions: totals, rankings, breakdowns, trends, comparisons.
- Look up specific records: find an invoice, a customer, a product by name or code.
- Explain Odoo concepts: what a field means, how a workflow works, what a status implies.
- Compare periods: this month vs last month, this year vs last year.
- Spot anomalies: spikes, drops, outliers, empty periods.
- Guide users: suggest which menu to open, which report to check, which filter to apply.
- Generate Excel: when the user asks to generate an Excel, export to Excel,
  create a spreadsheet, download Excel, اعمل اكسل, ملف اكسل, صدّر اكسل,
  نزّل اكسل, حوّل لاكسل, or any similar request — collect the data first,
  then call generate_excel with the columns and rows.

HOW YOU TALK
- Mirror the user's language: Arabic in -> Arabic out (Egyptian dialect is very
  welcome), English in -> English out. Keep numbers and dates in Latin digits.
- Never paste raw tool output at the user, and never show model names, domains
  or field names unless they ask. Say "invoices", not "account.move".

RESPONSE FORMAT (follow this structure for EVERY data answer)
Your reply MUST follow this exact layout, in order:

1. **📌 Headline** — One bold opening line with the key number or verdict.
   Example: **إجمالي المبيعات هذا الشهر: 1,250,000.00 جنيه**

2. **📊 Breakdown table** — A clean markdown table with:
   - Right-aligned numbers with thousands separators and 2 decimals.
   - Column headers in bold.
   - Maximum 10 rows; if trimmed say "(أعلى 10 من 43)" or "(top 10 of 43)".
   - Always include a **Total / الإجمالي** row at the bottom when showing amounts.
   Example:
   | الفرع | عدد الفواتير | الإجمالي |
   |:------|:-----------:|----------:|
   | القاهرة | 45 | 520,000.00 |
   | الإسكندرية | 32 | 380,000.00 |
   | **الإجمالي** | **77** | **900,000.00** |

3. **💡 Insight** (optional) — If the data shows something genuinely notable
   (a spike, a drop, an outlier, a zero period), mention it in one short line.
   Do NOT invent insights. Skip this section entirely if nothing stands out.
   Example: 💡 فرع أسيوط لم يسجل أي فواتير هذا الشهر.

4. **📎 Follow-up** — End with exactly ONE concrete follow-up question.
   Example: هل تحب أشوفلك تفاصيل فرع القاهرة؟

5. **📥 Excel offer** — After EVERY data answer that has a table, add this line:
   📥 **هل تحب أصدّرلك البيانات دي في ملف Excel؟**
   (or in English: 📥 **Would you like me to export this data to an Excel file?**)
   If the user says yes, call generate_excel immediately with the same data.

6. **🔍 Source line** — One quiet closing line in italics naming what you
   looked at and which period.
   Example: *بيانات فواتير المبيعات المؤكدة — من 2025-01-01 إلى 2025-12-31*

COMPARISON FORMAT (when comparing two periods or groups)
- Show BOTH periods side-by-side in one table.
- Add a **التغيير / Change** column with amount AND percentage.
- Use ▲ for increase, ▼ for decrease, ● for no change.
  Example:
  | البيان | الشهر الحالي | الشهر السابق | التغيير |
  |:-------|:-----------:|:-----------:|---------:|
  | المبيعات | 500,000 | 420,000 | ▲ 80,000 (+19.0%) |

SINGLE RECORD LOOKUP FORMAT (when finding a specific record)
- Show key fields as a short list, not a table:
  📌 **فاتورة INV/2025/0042**
  - العميل: شركة النور
  - التاريخ: 2025-03-15
  - الإجمالي: 125,000.00 جنيه
  - الحالة: مدفوعة ✅

FORMATTING RULES (CRITICAL — follow these EXACTLY)
- NEVER paste raw tool output, raw JSON, pipe-delimited lines, or field:aggregate
  syntax to the user. You must ALWAYS reformat data into clean markdown tables.
- NEVER show technical names: no model names (pos.order), no field names
  (amount_total:sum), no domain syntax, no "Totals over shown groups".
- Format money: thousands separator, 2 decimals (1,250,000.00).
- Format quantities: thousands separator, no decimals (1,250).
- Format percentages: 1 decimal (19.0%).
- Bold the most important number in the headline.
- At most one emoji per section header, never in error messages.
- Tables must have:
  * Clear human-readable column headers (not field names).
  * Proper markdown table syntax with | and --- separators.
  * Right-aligned numbers, left-aligned text.
  * A bold **Total** row at the bottom when showing amounts.
  * Maximum 15 rows. If more, show "top 15 of N" note.
- Replace technical values with human labels:
  * "pos.order" → "نقطة البيع / POS"
  * "sale.order" → "أمر بيع / Sales Order"
  * "account.move" → "فاتورة / Invoice"
  * Payment method IDs → actual payment method names.
  * Partner IDs → partner names.
  * Product IDs → product names.
- Keep the whole response SHORT — answer then details, never a wall of text.
- NEVER dump multiple raw query results one after another. Merge and summarize.

QUESTION TYPES AND STRATEGIES
1. **Data totals / rankings**: Use run_report if a pinned report fits, otherwise
   query_data with group_by + aggregates. Prefer aggregates over fetching all records.
2. **Find a specific record**: Use search_by_name to locate it, then query_data for
   its details.
3. **"What values exist?"**: Use get_field_values to discover branches, states,
   categories, payment methods etc. before building a domain.
4. **"How many?"**: Use count_records for a quick count before deciding whether to
   fetch details.
5. **Comparisons (this vs last)**: Run two queries (or two run_report calls with
   different dates) and present the comparison.
6. **"How do I..." / "Where is..."**: Explain the Odoo menu path, workflow, or
   concept. You don't need tools for this -- use your knowledge of Odoo 18.
7. **"What can you do?"**: List your capabilities warmly and offer examples.
8. **"Export to Excel" / "generate an Excel" / "ملف اكسل" / "اعمل اكسل"**:
   Trigger words: excel, spreadsheet, export, download, اكسل, صدّر, نزّل,
   حوّل, generate an excel, create excel, make excel, export data, تصدير.
   First collect the data using query_data or multi_query, then call
   generate_excel with the results. Always include the download link.

MODEL RELATIONSHIPS (use these to navigate between data)
- sale.order -> account.move (invoice_ids field) -> account.payment (reconciled)
- sale.order -> stock.picking (picking_ids) -> stock.move -> stock.move.line
- purchase.order -> account.move (invoice_ids) -> account.payment
- purchase.order -> stock.picking (picking_ids)
- pos.order -> pos.order.line, pos.payment, account.move (account_move)
- account.move -> account.move.line (line_ids)
- product.template -> product.product (product_variant_ids)
- hr.employee -> hr.contract, hr.payslip, hr.attendance, hr.leave
- stock.quant: current on-hand qty per (product, location, lot)
- stock.valuation.layer: cost/value per stock move

WHEN SOMETHING GOES WRONG
- Never repeat raw error text. Apologise in half a sentence, say in plain words
  what is missing (no access, module not installed, nothing in that period),
  and immediately offer something you CAN do instead.
- If you truly cannot tell what they mean, ask one short question rather than
  guessing.

SPEED RULES (critical for fast answers)
- For the common models listed in "COMMON MODEL FIELDS" in the context,
  you ALREADY know the field names. DO NOT call describe_model for them.
  Go straight to query_data or multi_query.
- When a question needs data from 2+ models (e.g. POS + Sales), use
  multi_query to run ALL queries in ONE call instead of calling query_data
  multiple times. This is MUCH faster.
- Only call describe_model for models NOT in the common fields list.

HOW YOU WORK (silently -- never narrate your tool calls)
1. Decide which data answers the question.
2. If a pinned report fits, call run_report -- it is pre-audited and fast.
3. For common models (see context), skip describe_model and go straight
   to query_data or multi_query.
4. For uncommon models, call describe_model first, then query_data.
5. When you need data from multiple models, use multi_query to batch them.
6. Use search_models when you are unsure which model holds the data.
7. Use get_field_values to discover valid values before filtering.
8. Use count_records to check data volume before fetching.
9. Use search_by_name to find specific records by name or reference.
10. Prefer group_by + aggregates over long record lists for totals and rankings.

HARD RULES
- Every number must come from tool output. Never estimate, never invent, never
  fill a gap from memory.
- Results are already filtered by the user's own Odoo access rights.
- ALWAYS follow the RESPONSE FORMAT above. Every data answer must have:
  headline, table (if applicable), follow-up question, and source line.
- When comparing periods, always use the COMPARISON FORMAT with ▲▼● arrows.
- When looking up a single record, use the SINGLE RECORD LOOKUP FORMAT.
- Never send a plain paragraph of numbers — always use a table or a list.
- ABSOLUTELY NEVER paste raw tool output. If a tool returns pipe-separated
  text, JSON, or "field:sum=123" format, you MUST rewrite it as a clean
  markdown table with human-readable headers before showing it to the user.
- If data comes from multiple sources (POS + Sales), create ONE unified
  table or summary — never paste two separate raw dumps.
"""


def _tool_schema():
    """OpenAI-compatible function definitions."""
    return [
        {
            'type': 'function',
            'function': {
                'name': 'search_models',
                'description': 'Find Odoo technical model names by keyword when '
                               'you do not know which model holds the data.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'keyword': {
                            'type': 'string',
                            'description': 'Word to search in model names/labels, '
                                           'e.g. "payslip", "picking", "pos".',
                        },
                    },
                    'required': ['keyword'],
                },
            },
        },
        {
            'type': 'function',
            'function': {
                'name': 'describe_model',
                'description': 'List the readable fields of a model (name, type, '
                               'relation). Call this before query_data.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'model': {
                            'type': 'string',
                            'description': 'Technical model name, e.g. "sale.order".',
                        },
                    },
                    'required': ['model'],
                },
            },
        },
        {
            'type': 'function',
            'function': {
                'name': 'query_data',
                'description': 'Read data from any allowed model. Use group_by + '
                               'aggregates for totals/rankings, or fields for a '
                               'record list. Runs with the user access rights.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'model': {
                            'type': 'string',
                            'description': 'Technical model name, e.g. "account.move".',
                        },
                        'domain': {
                            'type': 'string',
                            'description': 'Odoo domain as a JSON array, e.g. '
                                           '[["state","=","posted"],'
                                           '["invoice_date",">=","2025-01-01"]]. '
                                           'Use [] for no filter.',
                        },
                        'fields': {
                            'type': 'array',
                            'items': {'type': 'string'},
                            'description': 'Columns for a record list (ignored when '
                                           'group_by is used). Max 12.',
                        },
                        'group_by': {
                            'type': 'array',
                            'items': {'type': 'string'},
                            'description': 'Stored fields to group by. Date fields '
                                           'accept a granularity, e.g. '
                                           '"invoice_date:month".',
                        },
                        'aggregates': {
                            'type': 'array',
                            'items': {'type': 'string'},
                            'description': 'Measures like "amount_total:sum", '
                                           '"price_unit:avg". Record count is '
                                           'always included.',
                        },
                        'order': {
                            'type': 'string',
                            'description': 'Sort clause, e.g. "amount_total desc".',
                        },
                        'limit': {
                            'type': 'integer',
                            'description': 'Max rows/groups to return (default 50).',
                        },
                    },
                    'required': ['model'],
                },
            },
        },
        {
            'type': 'function',
            'function': {
                'name': 'run_report',
                'description': 'Run one of the curated pinned reports listed in the '
                               'system context.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'report': {'type': 'string',
                                   'description': 'Pinned report technical name.'},
                        'date_from': {'type': 'string',
                                      'description': 'Start date YYYY-MM-DD.'},
                        'date_to': {'type': 'string',
                                    'description': 'End date YYYY-MM-DD.'},
                        'limit': {'type': 'integer',
                                  'description': 'Row limit for ranking reports.'},
                    },
                    'required': ['report'],
                },
            },
        },
        {
            'type': 'function',
            'function': {
                'name': 'get_field_values',
                'description': 'Get the distinct values of a field on a model '
                               '(e.g. all branch names, all order states, all '
                               'product categories). Useful BEFORE building a '
                               'domain filter to know what values exist. '
                               'Returns up to 50 unique values.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'model': {
                            'type': 'string',
                            'description': 'Technical model name.',
                        },
                        'field': {
                            'type': 'string',
                            'description': 'Field name to get values of, e.g. '
                                           '"state", "branch_id", "categ_id".',
                        },
                        'domain': {
                            'type': 'string',
                            'description': 'Optional domain to pre-filter records '
                                           'before getting distinct values. JSON array.',
                        },
                        'limit': {
                            'type': 'integer',
                            'description': 'Max distinct values (default 50).',
                        },
                    },
                    'required': ['model', 'field'],
                },
            },
        },
        {
            'type': 'function',
            'function': {
                'name': 'count_records',
                'description': 'Count records matching a domain without fetching '
                               'them. Use this to quickly check how much data exists '
                               'before deciding to fetch details.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'model': {
                            'type': 'string',
                            'description': 'Technical model name.',
                        },
                        'domain': {
                            'type': 'string',
                            'description': 'Odoo domain as JSON array. Use [] for all.',
                        },
                    },
                    'required': ['model'],
                },
            },
        },
        {
            'type': 'function',
            'function': {
                'name': 'search_by_name',
                'description': 'Find records by name, reference, or display_name. '
                               'Returns id, display_name, and a few key fields. '
                               'Use this when the user asks about a specific '
                               'customer, product, invoice, order, etc.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'model': {
                            'type': 'string',
                            'description': 'Technical model name.',
                        },
                        'name': {
                            'type': 'string',
                            'description': 'Text to search for (searches name, '
                                           'display_name, and ref/default_code '
                                           'if available).',
                        },
                        'limit': {
                            'type': 'integer',
                            'description': 'Max results (default 10).',
                        },
                    },
                    'required': ['model', 'name'],
                },
            },
        },
        {
            'type': 'function',
            'function': {
                'name': 'multi_query',
                'description': 'Run 2-5 data queries in ONE call. MUCH faster than '
                               'calling query_data multiple times. Use this whenever '
                               'you need data from more than one model or more than '
                               'one slice of the same model (e.g. POS sales + SO sales, '
                               'or current month + previous month).',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'queries': {
                            'type': 'array',
                            'items': {
                                'type': 'object',
                                'properties': {
                                    'label': {
                                        'type': 'string',
                                        'description': 'Short label, e.g. "SO sales" '
                                                       'or "POS this month".',
                                    },
                                    'model': {
                                        'type': 'string',
                                        'description': 'Technical model name.',
                                    },
                                    'domain': {
                                        'type': 'string',
                                        'description': 'Odoo domain as JSON array.',
                                    },
                                    'fields': {
                                        'type': 'array',
                                        'items': {'type': 'string'},
                                        'description': 'Columns for record list.',
                                    },
                                    'group_by': {
                                        'type': 'array',
                                        'items': {'type': 'string'},
                                        'description': 'Fields to group by.',
                                    },
                                    'aggregates': {
                                        'type': 'array',
                                        'items': {'type': 'string'},
                                        'description': 'Measures like "amount_total:sum".',
                                    },
                                    'order': {
                                        'type': 'string',
                                        'description': 'Sort clause.',
                                    },
                                    'limit': {
                                        'type': 'integer',
                                        'description': 'Max rows (default 50).',
                                    },
                                },
                                'required': ['label', 'model'],
                            },
                            'description': 'Array of 2-5 queries to run at once.',
                        },
                    },
                    'required': ['queries'],
                },
            },
        },
        {
            'type': 'function',
            'function': {
                'name': 'generate_excel',
                'description': 'Create a downloadable Excel file from data you '
                               'already collected. Call this when the user asks '
                               'to generate an Excel, export to Excel, create a '
                               'spreadsheet, download data, اعمل اكسل, ملف اكسل, '
                               'صدّر اكسل, نزّل اكسل, or says yes to the Excel '
                               'offer. Pass the data as columns and rows.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'title': {
                            'type': 'string',
                            'description': 'Sheet name / report title, e.g. '
                                           '"Sales by Product Q1 2025".',
                        },
                        'columns': {
                            'type': 'array',
                            'items': {'type': 'string'},
                            'description': 'Column headers in order, e.g. '
                                           '["Product", "Qty", "Total"].',
                        },
                        'rows': {
                            'type': 'array',
                            'items': {
                                'type': 'array',
                                'items': {},
                            },
                            'description': 'Data rows as arrays matching columns '
                                           'order, e.g. [["Widget",10,5000], ...].',
                        },
                    },
                    'required': ['title', 'columns', 'rows'],
                },
            },
        },
    ]


class AiInvoiceChatController(http.Controller):

    # ==================================================================
    # Session routes
    # ==================================================================
    @http.route('/ai_invoice_chat/new_session', type='json', auth='user', methods=['POST'])
    def new_session(self):
        session = request.env['ai.chat.session'].create({'name': 'New Chat'})
        return {'session_id': session.id, 'name': session.name}

    @http.route('/ai_invoice_chat/sessions', type='json', auth='user', methods=['POST'])
    def list_sessions(self):
        sessions = request.env['ai.chat.session'].search(
            [('user_id', '=', request.env.uid)],
            order='write_date desc',
            limit=30,
        )
        return [
            {
                'id': s.id,
                'name': s.name,
                'message_count': s.message_count,
                'write_date': str(s.write_date or ''),
            }
            for s in sessions
        ]

    @http.route('/ai_invoice_chat/history', type='json', auth='user', methods=['POST'])
    def session_history(self, session_id):
        session = request.env['ai.chat.session'].browse(int(session_id))
        if not session.exists() or session.user_id.id != request.env.uid:
            return []
        return [
            {'role': m.role, 'content': m.content}
            for m in session.message_ids
            if m.role in ('user', 'assistant')
        ]

    @http.route('/ai_invoice_chat/clear', type='json', auth='user', methods=['POST'])
    def clear_session(self, session_id):
        session = request.env['ai.chat.session'].browse(int(session_id))
        if session.exists() and session.user_id.id == request.env.uid:
            session.action_clear()
        return {'ok': True}

    @http.route('/ai_invoice_chat/rename', type='json', auth='user', methods=['POST'])
    def rename_session(self, session_id, name):
        session = request.env['ai.chat.session'].browse(int(session_id))
        if session.exists() and session.user_id.id == request.env.uid:
            session.name = name or 'Chat'
        return {'ok': True}

    # ==================================================================
    # Main entry point
    # ==================================================================
    @http.route('/ai_invoice_chat/send', type='json', auth='user', methods=['POST'])
    def send_message(self, session_id, message):
        env = request.env

        if session_id:
            session = env['ai.chat.session'].browse(int(session_id))
            if not session.exists():
                session = env['ai.chat.session'].create({'name': (message or 'Chat')[:60]})
        else:
            session = env['ai.chat.session'].create({'name': (message or 'Chat')[:60]})

        if session.message_count == 0 and message:
            session.name = message[:60]

        env['ai.chat.message'].create({
            'session_id': session.id,
            'role': 'user',
            'content': message,
        })

        trace = []
        reply = None
        api_key = (env['ir.config_parameter'].sudo().get_param(
            'ai_invoice_chat.openai_key', '')
            or os.environ.get('OPENROUTER_API_KEY', '')
            or os.environ.get('GROQ_API_KEY', ''))

        if api_key:
            reply = self._run_agent(session, message, api_key, trace)

        if not reply:
            data_context = self._keyword_context(message)
            reply = self._call_odoo_iap(
                self._plain_messages(session, message, data_context))
            if not reply:
                reply = self._formatted_fallback(data_context)
            trace.append({'tool': 'keyword_fallback', 'output': data_context[:2000]})

        env['ai.chat.message'].create({
            'session_id': session.id,
            'role': 'assistant',
            'content': reply,
            'report_data': json.dumps(trace, ensure_ascii=False, default=str)[:60000],
        })

        return {
            'reply': reply,
            'session_id': session.id,
            'session_name': session.name,
        }

    # ==================================================================
    # Agent loop
    # ==================================================================
    def _system_context(self):
        env = request.env
        Schema = env['ai.data.schema']
        Reports = env['invoice.query.engine']
        company = env.company
        parts = [
            'Session facts:',
            '  today            : %s' % date.today(),
            '  user             : %s' % env.user.name,
            '  company          : %s' % (company.display_name or ''),
            '  currency         : %s' % (company.currency_id.name or ''),
            '  allowed companies: %s' % ', '.join(env.companies.mapped('name')),
            '',
        ]
        try:
            parts.append(Schema.get_catalog_text())
        except Exception as exc:
            _logger.warning('catalog build failed: %s', exc)
        parts.append('')
        try:
            parts.append(Reports.list_reports())
        except Exception as exc:
            _logger.warning('report catalog failed: %s', exc)

        # Pre-loaded common model fields so the LLM can skip describe_model
        parts.append('')
        parts.append('COMMON MODEL FIELDS (skip describe_model for these):')
        parts.append('  sale.order: name, partner_id, user_id, date_order, '
                     'amount_total, amount_untaxed, amount_tax, state, '
                     'currency_id, company_id, team_id, order_line')
        parts.append('  sale.order.line: order_id, product_id, product_uom_qty, '
                     'price_unit, price_subtotal, price_total, discount, '
                     'salesman_id, state, company_id, currency_id')
        parts.append('  account.move: name, partner_id, invoice_date, '
                     'invoice_date_due, amount_total, amount_residual, '
                     'amount_untaxed, amount_tax, state, move_type, '
                     'payment_state, currency_id, company_id, invoice_user_id')
        parts.append('  account.move.line: move_id, product_id, quantity, '
                     'price_unit, price_subtotal, price_total, discount, '
                     'account_id, partner_id, debit, credit, balance')
        parts.append('  pos.order: name, partner_id, user_id, date_order, '
                     'amount_total, amount_paid, amount_return, amount_tax, '
                     'state, session_id, company_id, config_id, lines, '
                     'payment_ids, employee_id')
        parts.append('  pos.order.line: order_id, product_id, qty, '
                     'price_unit, price_subtotal, price_subtotal_incl, '
                     'discount, product_uom_id')
        parts.append('  pos.payment: pos_order_id, amount, payment_date, '
                     'payment_method_id, session_id')
        parts.append('  product.product: name, default_code, barcode, '
                     'list_price, standard_price, categ_id, type, active, '
                     'product_tmpl_id, qty_available, virtual_available')
        parts.append('  product.template: name, default_code, list_price, '
                     'standard_price, categ_id, type, active, '
                     'attribute_line_ids, product_variant_ids')
        parts.append('  res.partner: name, email, phone, mobile, city, '
                     'country_id, company_id, customer_rank, supplier_rank, '
                     'total_invoiced, credit, debit')
        parts.append('  stock.quant: product_id, location_id, quantity, '
                     'reserved_quantity, lot_id, company_id')

        return '\n'.join(parts)

    def _run_agent(self, session, user_message, api_key, trace):
        messages = [
            {'role': 'system', 'content': _SYSTEM_PROMPT},
            {'role': 'system', 'content': self._system_context()},
        ]
        for hist in session.get_history(limit=20)[:-1]:
            if hist['role'] in ('user', 'assistant'):
                messages.append({'role': hist['role'], 'content': hist['content']})
        messages.append({'role': 'user', 'content': user_message})

        tools = _tool_schema()
        desc_cache = {}  # cache describe_model results per model name
        for _round in range(MAX_TOOL_ROUNDS):
            msg = self._openai_request(messages, api_key, tools)
            if msg is None:
                return None
            tool_calls = msg.get('tool_calls') or []
            if not tool_calls:
                return (msg.get('content') or '').strip() or None

            messages.append({
                'role': 'assistant',
                'content': msg.get('content') or '',
                'tool_calls': tool_calls,
            })
            for call in tool_calls:
                fn = call.get('function') or {}
                name = fn.get('name') or ''
                try:
                    args = json.loads(fn.get('arguments') or '{}')
                except ValueError:
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                output = self._run_tool(name, args, desc_cache)
                trace.append({'tool': name, 'args': args, 'output': output[:4000]})
                messages.append({
                    'role': 'tool',
                    'tool_call_id': call.get('id'),
                    'name': name,
                    'content': output[:MAX_TOOL_OUTPUT],
                })

        messages.append({
            'role': 'system',
            'content': 'Tool budget reached. Answer now using the data already '
                       'collected, and state what is still missing.',
        })
        msg = self._openai_request(messages, api_key, tools=None)
        if msg:
            return (msg.get('content') or '').strip() or None
        return None

    def _run_tool(self, name, args, desc_cache=None):
        env = request.env
        try:
            if name == 'search_models':
                return env['ai.data.schema'].search_models(args.get('keyword') or '')
            if name == 'describe_model':
                model_name = args.get('model') or ''
                if desc_cache is not None and model_name in desc_cache:
                    return desc_cache[model_name]
                result = env['ai.data.schema'].describe_model(model_name)
                if desc_cache is not None:
                    desc_cache[model_name] = result
                return result
            if name == 'query_data':
                return env['ai.data.query.engine'].run_query({
                    'model': args.get('model'),
                    'domain': self._parse_domain(args.get('domain')),
                    'fields': args.get('fields') or [],
                    'group_by': args.get('group_by') or [],
                    'aggregates': args.get('aggregates') or [],
                    'order': args.get('order') or '',
                    'limit': args.get('limit'),
                })
            if name == 'run_report':
                return env['invoice.query.engine'].run_report(
                    args.get('report') or '',
                    args.get('date_from'),
                    args.get('date_to'),
                    int(args.get('limit') or 15),
                )
            if name == 'get_field_values':
                return env['ai.data.query.engine'].get_field_values(
                    args.get('model') or '',
                    args.get('field') or '',
                    self._parse_domain(args.get('domain')),
                    int(args.get('limit') or 50),
                )
            if name == 'count_records':
                return env['ai.data.query.engine'].count_records(
                    args.get('model') or '',
                    self._parse_domain(args.get('domain')),
                )
            if name == 'search_by_name':
                return env['ai.data.query.engine'].search_by_name(
                    args.get('model') or '',
                    args.get('name') or '',
                    int(args.get('limit') or 10),
                )
            if name == 'multi_query':
                queries = args.get('queries') or []
                results = []
                qe = env['ai.data.query.engine']
                for idx, q in enumerate(queries[:5]):
                    label = q.get('label') or ('Query %d' % (idx + 1))
                    try:
                        out = qe.run_query({
                            'model': q.get('model'),
                            'domain': self._parse_domain(q.get('domain')),
                            'fields': q.get('fields') or [],
                            'group_by': q.get('group_by') or [],
                            'aggregates': q.get('aggregates') or [],
                            'order': q.get('order') or '',
                            'limit': q.get('limit'),
                        })
                    except Exception as exc:
                        out = 'ERROR: %s' % exc
                    results.append('--- %s ---\n%s' % (label, out))
                return '\n\n'.join(results)
            if name == 'generate_excel':
                return self._generate_excel(
                    args.get('title') or 'Report',
                    args.get('columns') or [],
                    args.get('rows') or [],
                )
            return "Unknown tool '%s'." % name
        except Exception as exc:
            _logger.warning('AI tool %s failed: %s', name, exc)
            return 'TOOL ERROR: %s' % exc

    def _generate_excel(self, title, columns, rows):
        """Build an .xlsx in memory, save as ir.attachment, return download URL."""
        try:
            import openpyxl
            from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
        except ImportError:
            return ('Excel generation unavailable — openpyxl not installed. '
                    'Present the data as a markdown table instead.')

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = (title or 'Report')[:31]  # Excel sheet name max 31 chars

        # ---- styles ----
        header_font = Font(name='Calibri', bold=True, size=12, color='FFFFFF')
        header_fill = PatternFill(start_color='4472C4', end_color='4472C4',
                                  fill_type='solid')
        header_align = Alignment(horizontal='center', vertical='center',
                                 wrap_text=True)
        thin_border = Border(
            left=Side(style='thin'), right=Side(style='thin'),
            top=Side(style='thin'), bottom=Side(style='thin'),
        )
        num_align = Alignment(horizontal='right', vertical='center')

        # ---- title row ----
        ws.merge_cells(start_row=1, start_column=1,
                       end_row=1, end_column=max(len(columns), 1))
        title_cell = ws.cell(row=1, column=1, value=title)
        title_cell.font = Font(name='Calibri', bold=True, size=14,
                               color='1F4E79')
        title_cell.alignment = Alignment(horizontal='center')
        ws.row_dimensions[1].height = 30

        # ---- headers ----
        for col_idx, col_name in enumerate(columns, 1):
            cell = ws.cell(row=3, column=col_idx, value=col_name)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_align
            cell.border = thin_border

        # ---- data rows ----
        for row_idx, row_data in enumerate(rows, 4):
            if not isinstance(row_data, (list, tuple)):
                row_data = [row_data]
            for col_idx, value in enumerate(row_data, 1):
                cell = ws.cell(row=row_idx, column=col_idx, value=value)
                cell.border = thin_border
                if isinstance(value, (int, float)):
                    cell.alignment = num_align
                    cell.number_format = '#,##0.00' if isinstance(value, float) else '#,##0'

        # ---- auto-width ----
        for col_idx in range(1, len(columns) + 1):
            max_len = 0
            col_letter = openpyxl.utils.get_column_letter(col_idx)
            for row in ws.iter_rows(min_row=3, max_row=ws.max_row,
                                    min_col=col_idx, max_col=col_idx):
                for cell in row:
                    if cell.value:
                        max_len = max(max_len, len(str(cell.value)))
            ws.column_dimensions[col_letter].width = min(max_len + 4, 40)

        # ---- save to bytes ----
        buf = io.BytesIO()
        wb.save(buf)
        file_data = buf.getvalue()
        buf.close()

        # ---- create ir.attachment ----
        safe_name = ''.join(c if c.isalnum() or c in ' _-' else '_'
                            for c in title)[:50]
        filename = '%s.xlsx' % safe_name
        attachment = request.env['ir.attachment'].sudo().create({
            'name': filename,
            'type': 'binary',
            'datas': base64.b64encode(file_data).decode(),
            'mimetype': 'application/vnd.openxmlformats-officedocument'
                        '.spreadsheetml.sheet',
            'res_model': 'ai.chat.session',
            'public': False,
        })
        download_url = '/web/content/%d/%s' % (attachment.id, filename)

        return ('Excel file created: [%s](%s)\n'
                'Tell the user: تم إنشاء ملف Excel — اضغط على الرابط لتحميله.\n'
                'Include this download link in your response as a clickable '
                'markdown link.' % (filename, download_url))

    @staticmethod
    def _parse_domain(raw):
        if not raw:
            return []
        if isinstance(raw, (list, tuple)):
            return list(raw)
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return []
        return parsed if isinstance(parsed, list) else []

    # ==================================================================
    # Providers
    # ==================================================================
    def _openai_request(self, messages, api_key, tools=None):
        ICP = request.env['ir.config_parameter'].sudo()
        base_url = (ICP.get_param('ai_invoice_chat.base_url', '')
                    or DEFAULT_BASE_URL).rstrip('/')
        model = ICP.get_param('ai_invoice_chat.openai_model', '') or DEFAULT_MODEL

        body = {
            'model': model,
            'messages': messages,
            'temperature': 0.15,
            'max_tokens': 4096,
        }
        if tools:
            body['tools'] = tools
            body['tool_choice'] = 'auto'
            # parallel_tool_calls: only for OpenAI-native endpoints
            if 'openai.com' in base_url:
                body['parallel_tool_calls'] = True

        headers = {
            'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + api_key,
        }
        # OpenRouter requires extra headers
        if 'openrouter.ai' in base_url:
            headers['HTTP-Referer'] = 'https://tradelinestores-stage-40209081.dev.odoo.com'
            headers['X-Title'] = 'Tradeline AI Assistant'
        req = urllib.request.Request(
            base_url + '/chat/completions',
            data=json.dumps(body).encode('utf-8'),
            headers=headers,
            method='POST',
        )
        _logger.info('AI request -> %s/chat/completions model=%s tools=%s',
                     base_url, model, bool(tools))
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = json.loads(resp.read().decode('utf-8'))
                _logger.info('AI response OK, usage=%s', payload.get('usage'))
                return payload['choices'][0]['message']
        except urllib.error.HTTPError as http_err:
            detail = http_err.read().decode('utf-8', errors='replace')
            _logger.error('AI provider HTTP %s: %s', http_err.code, detail[:1500])
        except Exception as exc:
            _logger.error('AI provider call failed: %s', exc)
        return None

    def _plain_messages(self, session, current_message, data_context):
        msgs = [{'role': 'system', 'content': _SYSTEM_PROMPT}]
        if data_context:
            msgs.append({'role': 'system',
                         'content': 'Live data from the database:\n\n' + data_context})
        for hist in session.get_history(limit=20)[:-1]:
            if hist['role'] in ('user', 'assistant'):
                msgs.append({'role': hist['role'], 'content': hist['content']})
        msgs.append({'role': 'user', 'content': current_message})
        return msgs

    def _keyword_context(self, message):
        try:
            return request.env['invoice.query.engine'].get_context_for_message(message)
        except Exception as exc:
            _logger.warning('Pinned report engine error: %s', exc)
            return ''

    def _call_odoo_iap(self, messages):
        try:
            from odoo.addons.iap.tools import iap_tools
            prompt = '\n\n'.join(
                '[' + m['role'].upper() + ']: ' + (m.get('content') or '')
                for m in messages)
            result = iap_tools.iap_jsonrpc(
                'https://iap.odoo.com/iap/1/chat',
                method='call',
                params={'prompt': prompt, 'max_tokens': 1024, 'temperature': 0.3},
                timeout=30,
            )
            if isinstance(result, dict):
                text = result.get('response') or result.get('text') or ''
                return text.strip() or None
            if isinstance(result, str) and result.strip():
                return result.strip()
        except Exception as exc:
            _logger.info('Odoo IAP not available: %s', exc)
        return None

    def _formatted_fallback(self, data_context):
        if data_context and data_context.strip():
            return (
                "Here is what I found in your database. I am showing it as-is "
                "because no AI provider is configured yet -- add an API key in "
                "Settings > General Settings > Tradeline AI Assistant and I can "
                "explain it in plain words instead.\n"
                "لم يتم إعداد "
                "مزوّد الذكاء "
                "الاصطناعي، "
                "لذلك أعرض "
                "البيانات كما "
                "هي."
                "\n\n" + data_context
            )
        return (
            "Sorry -- I could not match that to any data yet. Try naming what you "
            "want and a period, for example: \"sales by branch this month\", "
            "\"stock on hand\", or \"top products this year\".\n"
            "جرّب تسألني "
            "مثلاً: \"مبيعات "
            "الفروع هذا "
            "الشهر\" أو \"أعلى "
            "المنتجات مبيع"
            "اً هذا العام\"."
        )

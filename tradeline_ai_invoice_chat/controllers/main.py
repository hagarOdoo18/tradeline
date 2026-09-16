# -*- coding: utf-8 -*-
"""
AI Assistant controller
=======================
Turns a natural-language question into answers over the WHOLE Odoo
database.  The AI never touches the database directly: it may only call
four audited tools, all executed with the current user's access rights.

    search_models(keyword)                  -> find the right model
    describe_model(model)                   -> list its readable fields
    query_data(model, domain, fields, ...)  -> search_read / read_group
    run_report(report, date_from, date_to)  -> curated pinned reports

Fallback chain: OpenAI-compatible API (tool calling) -> Odoo IAP ->
formatted raw data from the keyword-routed pinned reports.
"""
import json
import logging
import urllib.request
import urllib.error
from datetime import date

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 6
MAX_TOOL_OUTPUT = 9000
DEFAULT_BASE_URL = 'https://api.openai.com/v1'
DEFAULT_MODEL = 'gpt-4o-mini'

_SYSTEM_PROMPT = """You are the Tradeline AI Assistant -- a friendly colleague inside an Odoo 18
ERP who happens to be very good with data. You can answer about any part of the
system: sales, invoicing and payments, purchases, inventory, point of sale,
products, customers and vendors, employees, payroll, projects and manufacturing.

HOW YOU TALK
- Warm, natural and short. Write like a helpful colleague, not a report generator.
- Mirror the user's language: Arabic in -> Arabic out (Egyptian dialect is very
  welcome), English in -> English out. Keep numbers and dates in Latin digits.
- Open with the answer itself -- the headline number or a one-line verdict --
  then the details.
- Show breakdowns as a compact markdown table (| Column | Column |), about 10
  rows max, and say "top 10 out of 43" when you trim.
- Never paste raw tool output at the user, and never show model names, domains
  or field names unless they ask. Say "invoices", not "account.move".
- If something in the data is genuinely worth noticing (a spike, an outlier, an
  empty period), mention it in one short line. Do not invent insights.
- End with exactly one concrete follow-up offer, phrased as a question
  ("Want it split by branch?" / "تحب أقسّمها بالفرع؟").
- Then one quiet closing line in italics: what you looked at and which period.
- At most one emoji, only for genuinely good news, never in an error message.

WHEN SOMETHING GOES WRONG
- Never repeat raw error text. Apologise in half a sentence, say in plain words
  what is missing (no access, module not installed, nothing in that period),
  and immediately offer something you CAN do instead.
- If you truly cannot tell what they mean, ask one short question rather than
  guessing.

HOW YOU WORK (silently -- never narrate your tool calls)
1. Decide which data answers the question.
2. If a pinned report fits, call run_report -- it is pre-audited and fast.
3. Otherwise call describe_model first (never guess field names), then
   query_data with a precise domain.
4. Use search_models when you are unsure which model holds the data.
5. Prefer group_by + aggregates over long record lists for totals and rankings.

HARD RULES
- Every number must come from tool output. Never estimate, never invent, never
  fill a gap from memory.
- Results are already filtered by the user's own Odoo access rights.
- Format money and quantities with thousands separators and 2 decimals."""


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
    ]


class AiInvoiceChatController(http.Controller):

    # ==================================================================
    # Session routes (endpoints unchanged for backward compatibility)
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
        """Stored messages of a session (used when reopening a chat)."""
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
        api_key = env['ir.config_parameter'].sudo().get_param(
            'ai_invoice_chat.openai_key', '')

        if api_key:
            reply = self._run_agent(session, message, api_key, trace)

        if not reply:
            # No key, provider down, or tool loop exhausted -> curated data path
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
        except Exception as exc:                            # pragma: no cover
            _logger.warning('catalog build failed: %s', exc)
        parts.append('')
        try:
            parts.append(Reports.list_reports())
        except Exception as exc:                            # pragma: no cover
            _logger.warning('report catalog failed: %s', exc)
        return '\n'.join(parts)

    def _run_agent(self, session, user_message, api_key, trace):
        """OpenAI tool-calling loop. Returns the final answer, or None."""
        messages = [
            {'role': 'system', 'content': _SYSTEM_PROMPT},
            {'role': 'system', 'content': self._system_context()},
        ]
        for hist in session.get_history(limit=11)[:-1]:
            if hist['role'] in ('user', 'assistant'):
                messages.append({'role': hist['role'], 'content': hist['content']})
        messages.append({'role': 'user', 'content': user_message})

        tools = _tool_schema()
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
                output = self._run_tool(name, args)
                trace.append({'tool': name, 'args': args, 'output': output[:4000]})
                messages.append({
                    'role': 'tool',
                    'tool_call_id': call.get('id'),
                    'name': name,
                    'content': output[:MAX_TOOL_OUTPUT],
                })

        # Tool budget exhausted -> force a final answer without tools
        messages.append({
            'role': 'system',
            'content': 'Tool budget reached. Answer now using the data already '
                       'collected, and state what is still missing.',
        })
        msg = self._openai_request(messages, api_key, tools=None)
        if msg:
            return (msg.get('content') or '').strip() or None
        return None

    def _run_tool(self, name, args):
        """Dispatch one AI tool call. Never raises."""
        env = request.env
        try:
            if name == 'search_models':
                return env['ai.data.schema'].search_models(args.get('keyword') or '')
            if name == 'describe_model':
                return env['ai.data.schema'].describe_model(args.get('model') or '')
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
            return "Unknown tool '%s'." % name
        except Exception as exc:                            # pragma: no cover
            _logger.warning('AI tool %s failed: %s', name, exc)
            return 'TOOL ERROR: %s' % exc

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
            'temperature': 0.2,
            'max_tokens': 1500,
        }
        if tools:
            body['tools'] = tools
            body['tool_choice'] = 'auto'

        req = urllib.request.Request(
            base_url + '/chat/completions',
            data=json.dumps(body).encode('utf-8'),
            headers={
                'Content-Type': 'application/json',
                'Authorization': 'Bearer ' + api_key,
            },
            method='POST',
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                payload = json.loads(resp.read().decode('utf-8'))
                return payload['choices'][0]['message']
        except urllib.error.HTTPError as http_err:
            detail = http_err.read().decode('utf-8', errors='replace')
            _logger.warning('AI provider HTTP %s: %s', http_err.code, detail[:800])
        except Exception as exc:
            _logger.warning('AI provider call failed: %s', exc)
        return None

    def _plain_messages(self, session, current_message, data_context):
        msgs = [{'role': 'system', 'content': _SYSTEM_PROMPT}]
        if data_context:
            msgs.append({'role': 'system',
                         'content': 'Live data from the database:\n\n' + data_context})
        for hist in session.get_history(limit=11)[:-1]:
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
            from odoo.addons.iap.tools import iap_tools  # noqa: PLC0415
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
                "\u0644\u0645 \u064a\u062a\u0645 \u0625\u0639\u062f\u0627\u062f \u0645\u0632\u0648\u0651\u062f \u0627\u0644\u0630\u0643\u0627\u0621 \u0627\u0644\u0627\u0635\u0637\u0646\u0627\u0639\u064a\u060c \u0644\u0630\u0644\u0643 \u0623\u0639\u0631\u0636 \u0627\u0644\u0628\u064a\u0627\u0646\u0627\u062a \u0643\u0645\u0627 \u0647\u064a."
                "\n\n" + data_context
            )
        return (
            "Sorry -- I could not match that to any data yet. Try naming what you "
            "want and a period, for example: \"sales by branch this month\", "
            "\"stock on hand\", or \"top products this year\".\n"
            "\u062c\u0631\u0651\u0628 \u062a\u0633\u0623\u0644\u0646\u064a \u0645\u062b\u0644\u0627\u064b: \"\u0645\u0628\u064a\u0639\u0627\u062a \u0627\u0644\u0641\u0631\u0648\u0639 \u0647\u0630\u0627 \u0627\u0644\u0634\u0647\u0631\" \u0623\u0648 \"\u0623\u0639\u0644\u0649 \u0627\u0644\u0645\u0646\u062a\u062c\u0627\u062a \u0645\u0628\u064a\u0639\u0627\u064b \u0647\u0630\u0627 \u0627\u0644\u0639\u0627\u0645\"."
        )

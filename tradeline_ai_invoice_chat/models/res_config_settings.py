# -*- coding: utf-8 -*-
from odoo import api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # ------------------------------------------------------------------
    # AI provider -- stored in ir.config_parameter
    # (keys keep the ai_invoice_chat.* prefix for backward compatibility)
    # ------------------------------------------------------------------
    ai_invoice_chat_openai_key = fields.Char(
        string='AI API Key',
        config_parameter='ai_invoice_chat.openai_key',
        help='Secret key of an OpenAI-compatible provider (sk-...).\n'
             'Leave empty to fall back to Odoo IAP, then to raw pinned reports.',
    )
    ai_invoice_chat_openai_model = fields.Char(
        string='AI Model',
        config_parameter='ai_invoice_chat.openai_model',
        default='gpt-4o-mini',
        help='Model name, e.g. gpt-4o-mini, gpt-4o. The model MUST support '
             'tool/function calling for system-wide questions.',
    )
    ai_invoice_chat_base_url = fields.Char(
        string='API Base URL',
        config_parameter='ai_invoice_chat.base_url',
        default='https://api.openai.com/v1',
        help='OpenAI-compatible endpoint. Change it to use Azure OpenAI, '
             'DeepSeek, Groq, a local vLLM/Ollama gateway, etc.',
    )

    # ------------------------------------------------------------------
    # Data scope
    # ------------------------------------------------------------------
    ai_invoice_chat_max_rows = fields.Integer(
        string='Max Rows per Query',
        config_parameter='ai_invoice_chat.max_rows',
        default=100,
        help='Upper bound on rows/groups returned by a single AI query '
             '(hard ceiling: 500).',
    )
    ai_invoice_chat_allow_all_models = fields.Boolean(
        string='Allow Any Model',
        config_parameter='ai_invoice_chat.allow_all_models',
        help='By default the assistant can only read the curated business '
             'catalog (sales, invoicing, stock, POS, HR, ...). Enable this to '
             'let it reach any model in the database. Technical and credential '
             'models stay blocked, and the user access rights always apply.',
    )
    ai_invoice_chat_extra_models = fields.Char(
        string='Additional Models',
        config_parameter='ai_invoice_chat.extra_models',
        help='Comma-separated technical model names to add to the catalog, '
             'e.g. mrp.production, fleet.vehicle.',
    )

# -*- coding: utf-8 -*-
{
    'name': 'Tradeline AI Assistant',
    'version': '18.0.3.0.0',
    'category': 'Productivity',
    'summary': 'AI chatbot that answers questions over ALL Odoo data, not only invoices',
    'description': """
        Tradeline AI Assistant
        ======================
        A chat assistant that answers natural-language questions (Arabic or
        English) about ANY data in the system:

        * Sales orders, quotations, customers and salespeople
        * Invoices, credit notes, payments, journals, branches, overdue balances
        * Purchases and vendors
        * Inventory: transfers, moves, on-hand quantities, lots/serials
        * Point of Sale: orders, sessions, payment methods
        * Products, categories, pricelists
        * Employees, departments, attendances, payroll
        * Anything else in the database (optional "Allow Any Model" setting)

        How it works
        ------------
        The AI never touches the database directly. It can only call four
        audited tools, all executed through the ORM with the **current user's
        access rights** (ACL, record rules, multi-company and branch filters):

          search_models  -> find the right model
          describe_model -> list its readable fields
          query_data     -> validated search_read / read_group (no raw SQL)
          run_report     -> curated, pre-audited pinned reports

        Credential and technical models (ir.*, API keys, passwords) are hard
        blocked, whatever the settings.

        Arabic & UX
        -----------
        * Answers in the user's language (Arabic incl. Egyptian dialect, or English)
        * Arabic period parsing: النهاردة، امبارح، الشهر اللي فات، آخر ٣ شهور،
          الربع الحالي، السنة الماضية، أسماء الشهور، والأرقام العربية ٠-٩
        * RTL chat bubbles, real HTML tables, copy button, timestamps, quick asks
        * Arabic translation file (i18n/ar.po)

        Configure in Settings > General Settings > Tradeline AI Assistant:
          * API key + model of any OpenAI-compatible provider (tool calling required)
          * API base URL (OpenAI, Azure, DeepSeek, Groq, local gateway...)
          * Row limits and model scope
        Without a key it falls back to Odoo IAP, then to formatted raw reports.
    """,
    'author': 'Tradeline',
    'depends': [
        'account',
        'sale',
        'web',
        'branch',
        'iap',
        'base_setup',
    ],
    'data': [
        'security/ir.model.access.csv',
        'views/res_config_settings_views.xml',
        'views/chat_session_views.xml',
        'views/chat_menu.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'tradeline_ai_invoice_chat/static/src/css/chat.css',
            'tradeline_ai_invoice_chat/static/src/xml/chat_templates.xml',
            'tradeline_ai_invoice_chat/static/src/js/chat_widget.js',
        ],
    },
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
}

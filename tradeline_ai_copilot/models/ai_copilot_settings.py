from odoo import api, fields, models


class AICopilotSettings(models.Model):
    _name = "ai.copilot.settings"
    _description = "AI Copilot Settings"

    name = fields.Char(default="Default Settings", required=True)
    default_provider = fields.Selection(
        [("openai", "OpenAI"), ("claude", "Claude")],
        default="openai",
        required=True,
    )
    openai_api_key = fields.Char(groups="base.group_system")
    claude_api_key = fields.Char(groups="base.group_system")
    default_openai_model = fields.Char(default="gpt-4.1-mini")
    default_claude_model = fields.Char(default="claude-3-5-sonnet-latest")
    temperature = fields.Float(default=0.1)
    max_tokens = fields.Integer(default=1200)
    timeout_seconds = fields.Integer(default=45)
    max_preview_rows = fields.Integer(default=150)
    max_export_rows_csv = fields.Integer(default=10000)
    max_export_rows_xlsx = fields.Integer(default=50000)
    enable_charts = fields.Boolean(default=True)
    enable_csv = fields.Boolean(default=True)
    enable_xlsx = fields.Boolean(default=True)
    enable_pdf = fields.Boolean(default=False)
    enable_page_context = fields.Boolean(default=True)
    enable_history = fields.Boolean(default=True)
    enable_audit_logs = fields.Boolean(default=True)
    brand_color = fields.Char(default="#2563EB")
    hard_deny_model_prefixes = fields.Char(
        default="ir.,bus.,mail.channel,mail.guest,base.import,base.language.install"
    )
    hard_deny_field_patterns = fields.Char(
        default="password,token,api_key,secret,oauth,private_key,session"
    )
    sql_view_allowlist = fields.Text(
        help="One technical SQL view name per line. These views can be queried in read-only mode by admin-approved tools."
    )

    @api.model
    def get_singleton(self):
        settings = self.search([], limit=1)
        if not settings:
            settings = self.create({"name": "Default Settings"})
        return settings


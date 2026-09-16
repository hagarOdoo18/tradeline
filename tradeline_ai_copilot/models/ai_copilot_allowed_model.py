from odoo import fields, models


class AICopilotAllowedModel(models.Model):
    _name = "ai.copilot.allowed.model"
    _description = "AI Copilot Allowed Model"
    _order = "model_name"

    model_name = fields.Char(required=True, index=True)
    display_name = fields.Char()
    model_id = fields.Many2one("ir.model", ondelete="cascade", required=True, index=True)
    enabled = fields.Boolean(default=True)
    is_business_model = fields.Boolean(default=True)
    max_rows = fields.Integer(default=1000)
    allow_groupby = fields.Boolean(default=True)
    allow_export = fields.Boolean(default=True)
    allowed_fields_csv = fields.Text(help="Comma-separated field names that are explicitly allowed. Blank means auto-safe fields.")

    _sql_constraints = [
        ("ai_copilot_model_unique", "unique(model_name)", "Each model can appear once in the AI allowlist."),
    ]


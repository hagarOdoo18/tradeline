from odoo import fields, models


class ExecutiveFxRate(models.Model):
    _name = "tradeline.executive.fx.rate"
    _description = "Executive FX Snapshot"
    _order = "fetched_at desc, id desc"

    pair = fields.Char(required=True, index=True)
    rate = fields.Float(digits=(16, 8), required=True)
    change_pct = fields.Float(digits=(16, 6))
    source_name = fields.Char(default="Yahoo Finance", required=True)
    source_symbol = fields.Char(required=True)
    source_timestamp = fields.Datetime()
    fetched_at = fields.Datetime(default=fields.Datetime.now, required=True, index=True)
    is_stale = fields.Boolean(default=False, index=True)
    status = fields.Selection(
        [("ok", "OK"), ("error", "Error")],
        default="ok",
        required=True,
        index=True,
    )
    message = fields.Text()
    inverted_from_symbol = fields.Char()


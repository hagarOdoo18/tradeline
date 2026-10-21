from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError


class MCPSettings(models.Model):
    _inherit = "ai.copilot.settings"

    enable_mcp_reporting = fields.Boolean(
        string="Enable MCP reporting", default=False, groups="base.group_system",
        help="Disabled by default. Also requires the MCP Reporting User group and explicit model/field approval.")


class MCPModelPolicy(models.Model):
    _inherit = "ai.copilot.allowed.model"

    mcp_enabled = fields.Boolean(string="Allow MCP", default=False, groups="base.group_system")
    mcp_fields_csv = fields.Text(
        string="MCP fields", groups="base.group_system",
        help="Explicit comma-separated fields, including id. Blank exposes nothing. Further restricted by the existing Copilot field policy.")

    @api.constrains("mcp_enabled", "mcp_fields_csv")
    def _check_mcp_fields(self):
        for policy in self:
            if policy.mcp_enabled and "id" not in {v.strip() for v in (policy.mcp_fields_csv or "").split(",")}:
                raise ValidationError("MCP models require an explicit field list including id.")


class MCPServicePolicy(models.AbstractModel):
    _inherit = "ai.copilot.service"

    def _mcp_policy(self, name):
        policy = self.env["ai.copilot.allowed.model"].sudo().search([
            ("model_name", "=", name), ("enabled", "=", True), ("mcp_enabled", "=", True)], limit=1)
        if not policy or not policy.mcp_fields_csv:
            raise AccessError("This dataset is not approved for MCP reporting.")
        return policy

    def _check_model_access(self, model_name):
        # The flag only restricts access; it never grants permissions. Normal
        # Odoo chat execution does not use this context and stays unchanged.
        if self.env.context.get("tradeline_mcp_reporting"):
            self._mcp_policy(model_name)  # Do not auto-create policy rows in a read-only transaction.
        return super()._check_model_access(model_name)

    def _safe_fields_for_model(self, model_name, explicit_fields=None):
        result = super()._safe_fields_for_model(model_name, explicit_fields)
        if self.env.context.get("tradeline_mcp_reporting"):
            policy = self._mcp_policy(model_name)
            allowed = {v.strip() for v in policy.mcp_fields_csv.split(",")}
            result = [name for name in result if name in allowed]
        return result


class MCPQueryPolicy(models.AbstractModel):
    _inherit = "ai.copilot.query"

    def _mcp_mask_related_labels(self, result):
        if not self.env.context.get("tradeline_mcp_reporting"):
            return result
        # Odoo's Many2one.convert_to_read uses sudo() for display_name.
        # Recheck related RECORD visibility before returning either ID or label.
        meta = self._readable_fields(result["source"])
        redacted = {}
        for column in result["columns"]:
            field = meta.get(column.split(":")[0], {})
            if field.get("type") != "many2one":
                continue
            ids = {row[column][0] for row in result["rows"]
                   if isinstance(row.get(column), (list, tuple)) and row[column]}
            target, unused = self._model(field["relation"])
            with self._budget():
                visible = set(target.with_context(active_test=False).search([("id", "in", list(ids))]).ids)
            for row in result["rows"]:
                value = row.get(column)
                if isinstance(value, (list, tuple)) and value and value[0] not in visible:
                    row[column] = None
                    redacted[column] = redacted.get(column, 0) + 1
        if redacted:
            result["provenance"]["redacted_related_cells"] = redacted
            result["provenance"]["redaction_note"] = "Related record IDs/labels hidden by record rules are unavailable, not empty relationships. Measures are unchanged."
        return result

    def execute_query(self, spec, export=False):
        return self._mcp_mask_related_labels(super().execute_query(spec, export=export))

    def _analysis_result(self, *args, **kwargs):
        return self._mcp_mask_related_labels(super()._analysis_result(*args, **kwargs))

    def _readable_fields(self, name):
        result = super()._readable_fields(name)
        if self.env.context.get("tradeline_mcp_reporting"):
            # Many2one reads carry labels. Do not expose labels from an
            # unapproved related dataset, including users/technical models.
            filtered = {}
            for key, meta in result.items():
                if meta.get("type") == "many2one":
                    try:
                        related = self._service()._mcp_policy(meta["relation"])
                        self._model(meta["relation"])
                        label_fields = set(self._service()._safe_fields_for_model(meta["relation"]))
                        if related.allowed_fields_csv:
                            label_fields &= {v.strip() for v in related.allowed_fields_csv.split(",")}
                        if not {"name", "display_name"} & label_fields:
                            continue
                    except (AccessError, ValidationError):
                        continue
                filtered[key] = meta
            result = filtered
        return result

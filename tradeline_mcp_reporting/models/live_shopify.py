"""Fixed live order reader. Generic MCP queries cannot use this policy exception."""
from odoo import fields, models
from odoo.exceptions import AccessError, ValidationError


class MCPLiveShopify(models.AbstractModel):
    _inherit = "tradeline.mcp.reporting"

    def _live_shopify_enabled(self):
        settings = self.env["ai.copilot.settings"].sudo().search([], limit=1)
        return bool(settings.enable_mcp_live_shopify and self.env.user.has_group("base.group_system"))

    def _live_shopify_engine(self):
        self._assert_mcp_access()
        if not self._live_shopify_enabled():
            raise AccessError("Live Shopify MCP reporting is disabled or requires an existing Odoo administrator. Saved Odoo Shopify records are a separate source.")
        if "shopify.configuration" not in self.env:
            raise ValidationError("The Shopify connector is not installed. Live data is unavailable, not zero.")
        # Require the pre-existing INTERNAL Copilot policy. Never create/enable a
        # policy in this read-only request or enable generic external config reads.
        policy = self.env["ai.copilot.allowed.model"].sudo().search([
            ("model_name", "=", "shopify.configuration"), ("enabled", "=", True)], limit=1)
        if not policy:
            raise AccessError("The existing internal Shopify reporting policy is disabled or missing.")
        # Only this fixed reader gets the INTERNAL policy context. User identity,
        # company scope, ACLs and record rules remain unchanged (never sudo).
        # _shopify_instances rechecks administrator, metadata, ACLs and companies;
        # _shopify_transport allows two constant GraphQL reads and no refresh.
        return self.env["ai.copilot.query"].with_context(tradeline_mcp_reporting=False)

    def _live_shopify(self, spec):
        allowed = {"operation", "instance_id", "date_from", "date_to", "financial_status",
                   "fulfillment_status", "cancelled", "test_orders", "grain", "limit", "title"}
        if not isinstance(spec, dict) or set(spec) - allowed:
            raise ValidationError("Use fixed Shopify order-report options; URLs, credentials, GraphQL and arbitrary filters are not accepted.")
        operation = spec.get("operation", "stores")
        if not isinstance(operation, str) or operation not in {"stores", "connection", "orders_summary", "orders"}:
            raise ValidationError("Choose stores, connection, orders_summary or orders.")
        if "instance_id" in spec and (type(spec["instance_id"]) is not int or spec["instance_id"] < 1):
            raise ValidationError("Choose a positive Shopify instance ID from store discovery.")
        if "limit" in spec and (type(spec["limit"]) is not int or not 1 <= spec["limit"] <= 500):
            raise ValidationError("Use a result limit between 1 and 500.")
        if "title" in spec and (not isinstance(spec["title"], str) or len(spec["title"]) > 200):
            raise ValidationError("Use a report title up to 200 characters.")
        engine = self._live_shopify_engine()
        if operation == "stores":
            if set(spec) - {"operation", "title"}:
                raise ValidationError("Store discovery accepts only operation and title.")
            instances = engine._shopify_instances()
            if len(instances) > 20:
                raise ValidationError("More than 20 accessible Shopify instances; ask the administrator to narrow store access.")
            rows = [{"instance_id": r.id, "store": r.name, "company_id": r.company_id.id} for r in instances]
            scope = "Accessible Shopify store metadata only; no API connection has been tested. Select an instance and run connection before order reporting. Administrator-only store-wide access; existing ACLs/company rules apply."
            return {"source": "shopify.configuration", "title": "Accessible live Shopify stores",
                    "columns": ["instance_id", "store", "company_id"], "rows": rows,
                    "row_count": len(rows), "truncated": False,
                    "provenance": {"source_kind": "shopify_store_discovery", "scope": scope,
                                   "queried_at": fields.Datetime.to_string(fields.Datetime.now())}}
        result = engine.live_shopify(spec)
        if result.get("truncated") or result.get("cells_truncated"):
            raise ValidationError("Live Shopify result would be truncated. Narrow dates/statuses or increase the bounded result limit; partial totals are not published.")
        return result

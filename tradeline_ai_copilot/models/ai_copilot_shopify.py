"""Private live Shopify evidence. Fixed GraphQL queries only; no sync / mutations."""
import json
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import requests

from odoo import fields, models
from odoo.exceptions import AccessError, ValidationError


CONNECTION_QUERY = "query CopilotConnection { currentAppInstallation { accessScopes { handle } } shop { name ianaTimezone currencyCode } }"
ORDERS_QUERY = """query CopilotOrders($after: String, $filter: String!) {
  orders(first: 100, after: $after, query: $filter, sortKey: CREATED_AT) {
    pageInfo { hasNextPage endCursor }
    nodes { id name createdAt updatedAt cancelledAt test displayFinancialStatus displayFulfillmentStatus
      currentTotalPriceSet { shopMoney { amount currencyCode } }
      currentTotalTaxSet { shopMoney { amount currencyCode } }
      totalRefundedSet { shopMoney { amount currencyCode } }
    }
  }
}"""


class CopilotShopify(models.AbstractModel):
    _inherit = "ai.copilot.query"

    def _shopify_context(self):
        if "shopify.configuration" not in self.env:
            return {"live_available": False, "reason": "Shopify connector is not installed."}
        try:
            instances = self._shopify_instances()
        except (AccessError, ValidationError):
            return {"live_available": False, "reason": "Live store-wide reads require existing administrator and instance/company/policy access. Saved Shopify models retain normal Odoo permissions."}
        return {"live_available": bool(instances), "instances": [{"id": r.id, "name": r.name, "company_id": r.company_id.id} for r in instances[:20]],
                "scope": "Fixed live connection / order summary / order list queries. No live product, customer-identity or inventory API surface. Saved synced records are separate; use data_freshness. Never claim all of Shopify is covered."}

    def _shopify_instances(self, export=False):
        self._service()._assert_internal_user()
        # Direct store access cannot reproduce per-order Odoo branch record rules.
        # Limit this surface to existing administrators; never grant broader access.
        if not self.env.user.has_group("base.group_system"):
            raise AccessError("Live Shopify reporting is restricted to Odoo administrators; saved Shopify records still use your normal permissions.")
        model, unused = self._analysis_model("shopify.configuration", export)
        meta = self._readable_fields("shopify.configuration")
        if not {"name", "shop_name", "company_id", "version"} <= meta.keys():
            raise AccessError("Shopify instance metadata is unavailable under your data policy.")
        with self._budget():
            return model.search([("company_id", "in", self.env.companies.ids)], limit=21)

    def _shopify_transport(self, instance, query, variables, deadline):
        # Neither URLs nor GraphQL can originate from the model / prompt.
        if query not in {CONNECTION_QUERY, ORDERS_QUERY}:
            raise AccessError("Only fixed read-only Shopify queries are allowed.")
        host = (instance.shop_name or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.myshopify\.com", host):
            raise ValidationError("Live reporting requires the instance's exact *.myshopify.com store hostname, without a URL or path.")
        version = instance.version or ""
        if not re.fullmatch(r"20\d{2}-(01|04|07|10)", version):
            raise ValidationError("The Shopify instance needs a dated stable API version (YYYY-MM).")
        # Use the connector's EXISTING token without invoking helpers which write
        # refreshed credentials. An expired token fails safely; no token generation.
        token = instance.access_token
        if not token or (instance.token_expiry and instance.token_expiry <= fields.Datetime.now()):
            raise ValidationError("The existing Shopify connector token is missing or expired. Refresh it through the usual connector workflow; Copilot will not modify credentials or sync data.")
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            raise ValidationError("Live Shopify time budget reached; narrow the dates. No complete report was produced.")
        response = None
        try:
            response = requests.post("https://%s/admin/api/%s/graphql.json" % (host, version),
                headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json", "Shopify-Search-Query-Debug": "1"},
                json={"query": query, "variables": variables}, timeout=(min(5, remaining), min(12, remaining)),
                allow_redirects=False, stream=True)
            if response.status_code != 200:
                raise ValidationError("Shopify could not complete the read (HTTP %s). Check existing connector access / rate limits; no sync was run." % response.status_code)
            content = bytearray()
            for chunk in response.iter_content(65536):
                content.extend(chunk)
                if len(content) > 2000000 or time.monotonic() > deadline:
                    raise ValidationError("Shopify response exceeded the safe read budget; narrow the report.")
            payload = json.loads(content)
            if payload.get("errors") or not isinstance(payload.get("data"), dict):
                # Do not echo provider messages, response bodies or authentication.
                raise ValidationError("Shopify rejected the fixed read query. Check the installed app's read scopes and API version; no complete report was produced.")
            extensions = payload.get("extensions") or {}
            if any(item.get("warnings") for item in extensions.get("search", [])):
                raise ValidationError("Shopify did not accept the report filters; no complete report was produced.")
            return payload["data"], response.headers.get("X-Shopify-API-Version", version)
        except (requests.RequestException, ValueError, TypeError):
            raise ValidationError("Live Shopify read failed. No complete report was produced and no records were changed.") from None
        finally:
            if response is not None:
                response.close()

    def live_shopify(self, spec, export=False):
        if not isinstance(spec, dict) or set(spec) - {"analysis_type", "model", "operation", "instance_id", "date_from", "date_to", "financial_status", "fulfillment_status", "cancelled", "test_orders", "grain", "limit", "title", "chart"}:
            raise ValidationError("Invalid live Shopify request.")
        instances = self._shopify_instances(export)
        instance_id = spec.get("instance_id")
        if instance_id is not None and type(instance_id) is not int:
            raise ValidationError("Use a Shopify instance ID from business_context.")
        instance = instances.filtered(lambda r: r.id == instance_id) if instance_id else instances
        if len(instance) != 1:
            raise ValidationError("Choose one accessible Shopify instance; ask the user which store if there are several.")
        operation = spec.get("operation", "connection")
        if operation not in {"connection", "orders_summary", "orders"}:
            raise ValidationError("Choose connection, orders_summary or orders.")
        deadline, started = time.monotonic() + 25, fields.Datetime.to_string(fields.Datetime.now())
        connection, version = self._shopify_transport(instance, CONNECTION_QUERY, {}, deadline)
        scopes = {r["handle"] for r in (connection.get("currentAppInstallation") or {}).get("accessScopes", [])}
        shop = connection.get("shop") or {}
        query = dict(spec, analysis_type="live_shopify", model="shopify.configuration", instance_id=instance.id)
        coverage = [{"instance_id": instance.id, "store": instance.shop_name, "company_id": instance.company_id.id,
                     "api_version": version, "operation": operation}]
        scope = "Direct live Shopify API read, store-wide within this accessible company's instance; Odoo per-order branch restrictions cannot be applied. " \
                "Administrator-only. No imports, syncs, credential refresh, webhooks or mutations. No customer identities / addresses / notes retrieved. "
        if operation == "connection":
            rows = [{"store": shop.get("name"), "timezone": shop.get("ianaTimezone"), "currency": shop.get("currencyCode"),
                     "read_orders": bool(scopes & {"read_orders", "write_orders"}), "read_all_orders": "read_all_orders" in scopes,
                     "recorded_sync_marker_utc": str(instance.order_last_synced or "Unknown")}]
            scope += "Successful live connection is not proof that Odoo has imported all orders. Recorded connector sync markers are not a completeness guarantee."
        else:
            start, end = self._report_dates(spec)
            if (fields.Date.to_date(end) - fields.Date.to_date(start)).days > 366:
                raise ValidationError("Live order reporting is limited to a one-year window; narrower dates are faster.")
            if not scopes & {"read_orders", "write_orders"}:
                raise AccessError("The existing Shopify app does not have order-read access. No permissions were changed.")
            if "read_all_orders" not in scopes and fields.Date.to_date(start) < (datetime.now(timezone.utc) - timedelta(days=59)).date():
                raise ValidationError("The existing Shopify app cannot establish full older-order coverage without read_all_orders. Use a recent window or saved Odoo records; permissions were not changed.")
            # Day bounds are Shopify shop-local dates; explicit offsets prevent
            # UTC-midnight shifts. Do not pass arbitrary user search syntax.
            import pytz
            try:
                tz = pytz.timezone(shop["ianaTimezone"])
            except (KeyError, pytz.UnknownTimeZoneError):
                raise ValidationError("Shopify did not provide a valid store timezone.") from None
            lower = tz.localize(datetime.combine(fields.Date.to_date(start), datetime.min.time())).isoformat()
            upper = tz.localize(datetime.combine(fields.Date.to_date(end) + timedelta(days=1), datetime.min.time())).isoformat()
            filter_text = "created_at:>='%s' created_at:<'%s'" % (lower, upper)
            selections = {}
            for field, allowed in [("financial_status", {"any", "paid", "pending", "authorized", "partially_paid", "partially_refunded", "refunded", "voided"}),
                                   ("fulfillment_status", {"any", "fulfilled", "unfulfilled", "partial"}),
                                   ("cancelled", {"any", "yes", "no"}), ("test_orders", {"any", "yes", "no"})]:
                value = spec.get(field, "any")
                if value not in allowed:
                    raise ValidationError("Unsupported Shopify status filter.")
                selections[field] = value
            grain = spec.get("grain", "total")
            if grain not in {"total", "day", "month"}:
                raise ValidationError("Use total, day or month grouping.")
            orders, after = [], None
            for unused in range(20):
                data, page_version = self._shopify_transport(instance, ORDERS_QUERY, {"after": after, "filter": filter_text}, deadline)
                if version != page_version:
                    raise ValidationError("Shopify API version changed during the read; retry the report.")
                page = data.get("orders") or {}
                nodes, info = page.get("nodes"), page.get("pageInfo") or {}
                if not isinstance(nodes, list) or not isinstance(info.get("hasNextPage"), bool):
                    raise ValidationError("Incomplete Shopify pagination metadata; no complete report was produced.")
                orders.extend(nodes)
                if not info["hasNextPage"]:
                    break
                cursor = info.get("endCursor")
                if not cursor or cursor == after:
                    raise ValidationError("Shopify pagination did not advance; no complete report was produced.")
                after = cursor
            else:
                raise ValidationError("Live report exceeds 2,000 orders; narrow the dates. Partial totals were discarded.")
            if len({o.get("id") for o in orders}) != len(orders):
                raise ValidationError("Shopify orders changed during pagination; retry a narrower report.")
            buckets, detail = {}, []
            try:
                for order in orders:
                    values = [order[key]["shopMoney"] for key in ("currentTotalPriceSet", "currentTotalTaxSet", "totalRefundedSet")]
                    currency = values[0]["currencyCode"]
                    if any(v["currencyCode"] != currency for v in values):
                        raise ValueError("Currency mismatch")
                    amounts = [Decimal(v["amount"]) for v in values]
                    if not all(a.is_finite() for a in amounts):
                        raise ValueError("Nonfinite amount")
                    local_date = datetime.fromisoformat(order["createdAt"].replace("Z", "+00:00")).astimezone(tz).date()
                    if not fields.Date.to_date(start) <= local_date <= fields.Date.to_date(end):
                        raise ValueError("Date outside requested bounds")
                    period = str(local_date)[:7] if grain == "month" else str(local_date) if grain == "day" else start + " to " + end
                    financial, fulfillment = order["displayFinancialStatus"], order["displayFulfillmentStatus"]
                    expected = {"financial_status": str(financial).lower(),
                                "fulfillment_status": {"PARTIALLY_FULFILLED": "partial"}.get(fulfillment, str(fulfillment).lower()),
                                "cancelled": "yes" if order["cancelledAt"] else "no", "test_orders": "yes" if order["test"] else "no"}
                    if any(v != "any" and v != expected[k] for k, v in selections.items()):
                        continue
                    row = {"order": order["name"], "created_at_utc": order["createdAt"], "updated_at_utc": order["updatedAt"],
                        "financial_status": financial, "fulfillment_status": fulfillment, "cancelled": bool(order["cancelledAt"]),
                        "test": order["test"], "currency": currency, "current_order_value": float(amounts[0]),
                        "current_tax": float(amounts[1]), "lifetime_refunded": float(amounts[2])}
                    detail.append(row)
                    key = (period, currency, financial, fulfillment, row["cancelled"], row["test"])
                    if key not in buckets:
                        buckets[key] = {"period": period, "currency": currency, "financial_status": financial, "fulfillment_status": fulfillment,
                            "cancelled": row["cancelled"], "test": row["test"], "orders": 0,
                            "current_order_value": Decimal(0), "current_tax": Decimal(0), "lifetime_refunded": Decimal(0)}
                    target = buckets[key]
                    target["orders"] += 1
                    for k, amount in zip(("current_order_value", "current_tax", "lifetime_refunded"), amounts):
                        target[k] += amount
            except (KeyError, TypeError, ValueError, InvalidOperation):
                raise ValidationError("Shopify returned incomplete / inconsistent order evidence; no totals were published.") from None
            rows = detail if operation == "orders" else [{k: float(v) if isinstance(v, Decimal) else v for k, v in r.items()} for r in buckets.values()]
            coverage[0].update(created_date_from=start, created_date_to=end, timezone=shop["ianaTimezone"], filter=filter_text,
                               status_filters=selections, complete_orders_read=len(orders), qualifying_orders=len(detail))
            scope += "Order CREATED date in the shop timezone, all financial/fulfillment/cancellation/test statuses unless explicitly filtered. " \
                "Current order value includes discounts/taxes and reflects Shopify's current returns/edits; it is NOT recognized revenue, cash receipts or Shopify Analytics net sales. " \
                "Lifetime refunded is shown separately, can relate to other refund dates and must NOT be subtracted again. " \
                "Do not combine with Odoo invoice revenue or assume one-to-one imported orders. No COGS / margin is inferred. " \
                "Pagination covers the requested scope within existing API scopes; live changes during the read can affect consistency."
        columns = list(rows[0]) if rows else (["order", "currency", "current_order_value"] if operation == "orders" else ["period", "currency", "orders", "current_order_value"])
        labels = {k: k.replace("_", " ").capitalize() for k in columns}
        result = self._analysis_result(query, "shopify.configuration", "Live Shopify " + operation.replace("_", " "), rows, columns, labels, coverage, scope, export)
        result["provenance"]["freshness"] = {"mode": "Live Shopify API", "read_started_utc": started,
            "read_completed_utc": fields.Datetime.to_string(fields.Datetime.now()), "note": "Live fetch, not a saved Odoo sync or transactionally frozen snapshot."}
        result["provenance"]["source_kind"] = "live_shopify"
        result["provenance"]["active_test"] = False
        if operation == "orders_summary" and len({r["currency"] for r in result["rows"]}) == 1:
            for r in result["rows"]:
                r["segment"] = " / ".join([r["period"], r["financial_status"], r["fulfillment_status"],
                    "cancelled" if r["cancelled"] else "not cancelled", "test" if r["test"] else "non-test"])
            result["chart_dimensions"], result["chart_measures"] = ["segment"], ["current_order_value"]
            result["labels"]["segment"] = "Order status cohort"
            result["chart_unit"] = result["rows"][0]["currency"]
        return result

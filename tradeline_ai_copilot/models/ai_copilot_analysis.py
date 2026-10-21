"""Deterministic cross-module analytics. Only ORM reads; never model-written code."""
import json
import time
from collections import Counter, defaultdict
from itertools import combinations
from psycopg2.errors import QueryCanceled

from odoo import fields, models
from odoo.exceptions import AccessError, ValidationError


class CopilotAnalysis(models.AbstractModel):
    _inherit = "ai.copilot.query"

    ANALYSIS_GROUP_LIMIT = 50000
    PARENTS = {"account.move.line": "move_id", "legacy.invoice.line": "invoice_id",
               "sale.order.line": "order_id", "pos.order.line": "order_id"}

    def _analysis_model(self, name, export=False):
        model, policy = self._model(name)
        if not policy.allow_groupby:
            raise AccessError("Aggregation is disabled for an analysis source.")
        if export and not policy.allow_export:
            raise AccessError("Export is disabled for an analysis source.")
        return model, policy

    def _analysis_relation(self, name, field, target=None):
        meta = self._readable_fields(name).get(field)
        if not meta or meta["type"] != "many2one" or (target and meta["relation"] != target):
            raise ValidationError("Analysis keys must be readable, stored many-to-one relationships to the same entity.")
        self._model(meta["relation"])
        return meta["relation"]

    def _analysis_domain(self, name, domain, export=False):
        result = list(self._domain(name, domain))
        parent = self.PARENTS.get(name)
        if parent:
            related = self._analysis_relation(name, parent)
            parent_model, unused_policy = self._analysis_model(related, export)
            # ORM subquery applies header record rules as well as line rules.
            result.append((parent, "in", parent_model._search([])))
        return result

    def _analysis_groups(self, model, domain, groups, deadline):
        if time.monotonic() > deadline:
            raise ValidationError("Analysis time budget reached; narrow the dates or products.")
        try:
            with self._budget():
                rows = model.read_group(domain, [], groups, lazy=False, limit=self.ANALYSIS_GROUP_LIMIT + 1)
        except QueryCanceled as exc:
            raise ValidationError("This analysis exceeded the eight-second query budget. Narrow the date window, branch or product scope; do not infer absence from this failure.") from exc
        if len(rows) > self.ANALYSIS_GROUP_LIMIT:
            raise ValidationError("Analysis exceeds 50,000 distinct groups. Narrow the dates/products; incomplete sets cannot establish absence.")
        return rows

    def _customer_set(self, spec, export, deadline, identity_cache=None):
        if not isinstance(spec, dict) or set(spec) - {"model", "domain", "key_field", "resolve_legacy_links", "resolve_legacy_identifiers"}:
            raise ValidationError("Use a model, domain, customer key_field and optional legacy resolution flags.")
        for flag in ("resolve_legacy_links", "resolve_legacy_identifiers"):
            if flag in spec and type(spec[flag]) is not bool:
                raise ValidationError("Legacy resolution flags must be booleans.")
        name, key = spec.get("model"), spec.get("key_field", "partner_id")
        model, unused_policy = self._analysis_model(name, export)
        self._analysis_relation(name, key, "res.partner")
        domain = self._analysis_domain(name, spec.get("domain", []), export)
        rows = self._analysis_groups(model, domain, [key], deadline)
        keys = {row[key][0] for row in rows if row.get(key)}
        missing = sum(row["__count"] for row in rows if not row.get(key))
        total = sum(row["__count"] for row in rows)
        linked, ambiguous, identifier_linked, methods = 0, 0, 0, {}
        resolve = spec.get("resolve_legacy_links") or spec.get("resolve_legacy_identifiers")
        if resolve and (name != "legacy.invoice.line" or key != "partner_id"):
            raise ValidationError("Legacy resolution applies only to legacy invoice lines with partner_id as the customer key.")
        if missing and resolve:
            invoices = self._analysis_groups(model, domain + [(key, "=", False)], ["invoice_id"], deadline)
            counts = {r["invoice_id"][0]: r["__count"] for r in invoices if r.get("invoice_id")}
            mappings = self._legacy_bridge_mappings(counts, export, deadline) if spec.get("resolve_legacy_links") else {}
            for invoice_id, targets in mappings.items():
                if len(targets) == 1:
                    keys.update(targets)
                    linked += counts[invoice_id]
                else:
                    ambiguous += counts[invoice_id]
            if spec.get("resolve_legacy_identifiers"):
                matches, methods = self._legacy_identifier_matches(model, domain + [(key, "=", False)], counts,
                    set(mappings), export, deadline, identity_cache if identity_cache is not None else {})
                keys.update(matches.values())
                identifier_linked = sum(counts[invoice_id] for invoice_id in matches)
        return keys, {"model": name, "domain": spec.get("domain", []), "key_field": key,
                      "matching_rows": total, "unresolved_rows": missing - linked - identifier_linked,
                      "bridge_linked_rows": linked, "ambiguous_link_rows": ambiguous,
                      "identifier_matched_rows": identifier_linked, "identifier_resolution_rows": methods}

    def _legacy_bridge_mappings(self, counts, export, deadline):
        bridge, unused_policy = self._analysis_model("legacy.invoice.customer.bridge", export)
        for field, target in [("invoice_id", "legacy.invoice"), ("target_partner_id", "res.partner")]:
            self._analysis_relation(bridge._name, field, target)
        self._domain(bridge._name, [["match_status", "in", ["strict", "linked"]]])
        mappings = defaultdict(set)
        invoice_ids = list(counts)
        for start in range(0, len(invoice_ids), 500):
            matches = self._analysis_groups(bridge, [("invoice_id", "in", invoice_ids[start:start+500]),
                ("match_status", "in", ["strict", "linked"]), ("target_partner_id", "!=", False)],
                ["invoice_id", "target_partner_id"], deadline)
            for row in matches:
                mappings[row["invoice_id"][0]].add(row["target_partner_id"][0])
        return mappings


    def _visible_keys(self, model_name, keys, export, deadline, names=False):
        model, unused_policy = self._analysis_model(model_name, export)
        fields_to_read = ["id"]
        label_field = "name"
        if names:
            readable = self._readable_fields(model_name)
            label_field = "name" if "name" in readable else "display_name"
            if label_field not in readable:
                raise AccessError("Names are unavailable under the data policy.")
            fields_to_read.append(label_field)
        result, ids = {}, sorted(keys)
        for start in range(0, len(ids), 500):
            if time.monotonic() > deadline:
                raise ValidationError("Analysis time budget reached; narrow the scope.")
            with self._budget():
                rows = model.search_read([("id", "in", ids[start:start+500])], fields_to_read, limit=500)
            result.update({row["id"]: row.get(label_field, str(row["id"])) for row in rows})
        return result

    def _analysis_result(self, spec, source, label, rows, columns, labels, coverage, scope, export):
        unused_model, policy = self._analysis_model(source, export)
        requested = spec.get("limit", 50)
        if type(requested) is not int or requested < 1:
            raise ValidationError("Use a positive result limit.")
        limit = min(requested, 50000 if export else 500, max(1, policy.max_rows or 1000))
        provenance = {"model": source, "model_label": label, "queried_at": fields.Datetime.to_string(fields.Datetime.now()),
            "companies": [{"id": c.id, "name": c.name} for c in self.env.companies], "domain": [],
            "filters": [{"field": "Source definition", "operator": ":", "value": json.dumps(c, default=str)} for c in coverage],
            "fields": columns, "group_by": [], "aggregates": [], "labels": labels, "limit": limit,
            "returned_rows": min(limit, len(rows)), "truncated": len(rows) > limit,
            "active_test": bool(self.env.context.get("active_test", True)), "scope": scope, "analysis_sources": coverage}
        return {"query": {"model": source, **spec}, "source": source, "title": spec.get("title") or label,
            "columns": columns, "labels": labels, "rows": rows[:limit], "row_count": min(limit, len(rows)),
            "truncated": len(rows) > limit, "cells_truncated": False, "units": {}, "provenance": provenance,
            "analysis": {"scope": scope, "sources": coverage}}

    def compare_customer_sets(self, spec, export=False):
        self._service()._assert_internal_user()
        if not isinstance(spec, dict) or set(spec) - {"analysis_type", "model", "include", "exclude", "show_customers", "include_names", "limit", "title", "chart"}:
            raise ValidationError("Invalid customer cohort definition.")
        included, excluded = spec.get("include"), spec.get("exclude", [])
        if not isinstance(included, list) or not 1 <= len(included) <= 4 or not isinstance(excluded, list) or len(excluded) > 4:
            raise ValidationError("Use one to four include sources and up to four exclusion sources.")
        deadline, populations, coverage = time.monotonic() + 25, [set(), set()], []
        identity_cache, source_cache = {}, {}
        for index, sources in enumerate([included, excluded]):
            for source in sources:
                source_key = json.dumps(source, sort_keys=True, default=str)
                if source_key not in source_cache:
                    source_cache[source_key] = self._customer_set(source, export, deadline, identity_cache)
                keys, detail = source_cache[source_key]
                populations[index].update(keys)
                coverage.append(dict(detail, role="include" if index == 0 else "exclude"))
        # Customer record rules apply even if an invoice-line relationship is visible.
        visible = self._visible_keys("res.partner", populations[0] | populations[1], export, deadline,
                                     names=bool(spec.get("show_customers") and spec.get("include_names")))
        left, right = (keys & visible.keys() for keys in populations)
        eligible = left - right
        summary = {"included_customers": len(left), "also_in_excluded_set": len(left & right),
                   "eligible_customers": len(eligible), "unresolved_source_rows": sum(c["unresolved_rows"] for c in coverage)}
        unresolved_exclusions = sum(c["unresolved_rows"] for c in coverage if c["role"] == "exclude")
        summary["unresolved_exclusion_rows"] = unresolved_exclusions
        scope = ("Union of include sources minus union of exclusion sources, deduplicated by exact saved res.partner ID. "
                 "Legacy resolution can use saved strict/linked bridges and optional unique exact normalized identifiers; no names or approximate matching. "
                 "Computed matches compare Egyptian mobile formats or explicitly international phones, and structurally valid Egyptian national IDs (individual VAT only). "
                 "National IDs are not authenticated. Shared identifiers and conflicting signals remain unresolved. Uniqueness is within readable current contacts including archived contacts. "
                 "Unresolved rows are excluded from customer counts and are NOT customers who did not upgrade. "
                 "Duplicate contacts are not merged. Absence means no matching purchase in these visible sources and filters, not no purchase elsewhere. "
                 "If unresolved_exclusion_rows is nonzero, eligible_customers are PROVISIONAL candidates: an unresolved purchase might belong to them. "
                 "Returned merchandise is not netted automatically. Summary: " + json.dumps(summary))
        if spec.get("show_customers"):
            rows = [{"customer_id": key, **({"customer_name": visible[key]} if spec.get("include_names") else {})} for key in sorted(eligible)]
            columns = ["customer_id"] + (["customer_name"] if spec.get("include_names") else [])
            labels = {"customer_id": "Customer ID", "customer_name": "Customer"}
        else:
            metric_labels = {"included_customers": "Customers with qualifying purchases",
                "also_in_excluded_set": "Customers also in the exclusion set",
                "eligible_customers": "Provisional candidates" if unresolved_exclusions else "Customers with no linked exclusion purchase",
                "unresolved_source_rows": "Unresolved source rows (definitions may overlap)",
                "unresolved_exclusion_rows": "Unresolved exclusion rows"}
            rows = [{"metric": metric_labels[key], "count": value} for key, value in summary.items()]
            columns, labels = ["metric", "count"], {"metric": "Measure", "count": "Count"}
        result = self._analysis_result(dict(spec, analysis_type="customer_sets"), "res.partner", "Cross-module customer cohort",
                                       rows, columns, labels, coverage, scope, export)
        result["analysis"].update(summary)
        result["analysis"]["identity_coverage_complete"] = summary["unresolved_source_rows"] == 0
        result["analysis"]["absence_established_within_matched_scope"] = unresolved_exclusions == 0
        return result

    def analyze_baskets(self, spec, export=False):
        self._service()._assert_internal_user()
        if not isinstance(spec, dict) or set(spec) - {"analysis_type", "model", "domain", "transaction_field", "product_field", "limit", "title", "chart"}:
            raise ValidationError("Invalid basket definition.")
        name = spec.get("model")
        model, unused_policy = self._analysis_model(name, export)
        transaction, product = spec.get("transaction_field"), spec.get("product_field", "product_id")
        target = self._analysis_relation(name, transaction)
        self._analysis_model(target, export)
        self._analysis_relation(name, product, "product.product")
        domain = self._analysis_domain(name, spec.get("domain", []), export)
        # Apply transaction rules for custom sources too.
        domain.append((transaction, "in", self.env[target]._search([])))
        deadline = time.monotonic() + 25
        groups = self._analysis_groups(model, domain, [transaction, product], deadline)
        products = {r[product][0] for r in groups if r.get(product)}
        visible = self._visible_keys("product.product", products, export, deadline, names=True)
        baskets, unmapped = defaultdict(set), 0
        for row in groups:
            if row.get(transaction) and row.get(product) and row[product][0] in visible:
                baskets[row[transaction][0]].add(row[product][0])
            else:
                unmapped += row["__count"]
        oversized = sum(len(items) > 30 for items in baskets.values())
        valid = [items for items in baskets.values() if len(items) <= 30]
        if sum(len(items) * (len(items) - 1) // 2 for items in valid) > 200000:
            raise ValidationError("Basket pair budget reached; narrow the date window or branch.")
        singles, pairs = Counter(), Counter()
        for items in valid:
            singles.update(items)
            pairs.update(combinations(sorted(items), 2))
        count = len(valid)
        rows = [{"product_a": visible[a], "product_b": visible[b], "baskets_together": n,
                 "support_pct": round(100 * n / count, 2), "a_to_b_pct": round(100 * n / singles[a], 2),
                 "lift": round(n * count / (singles[a] * singles[b]), 3)}
                for (a, b), n in pairs.most_common()]
        columns = ["product_a", "product_b", "baskets_together", "support_pct", "a_to_b_pct", "lift"]
        labels = dict(zip(columns, ["Product A", "Product B", "Baskets together", "Support %", "A → B %", "Lift"]))
        coverage = [{"model": name, "domain": spec.get("domain", []), "transaction_field": transaction,
                     "product_field": product, "analyzed_baskets": count, "unmapped_rows": unmapped,
                     "oversized_baskets_excluded": oversized}]
        scope = ("Same-document co-purchases; duplicate product lines count once per basket. Support uses analyzed visible baskets, including single-product baskets. "
                 "A → B is the fraction of A baskets containing B. Lift compares observed co-purchases with independent purchase frequency; association is not causation. "
                 "Baskets over 30 distinct products are excluded. Missing product mappings may understate associations. "
                 "Use positive purchase quantities and posted/confirmed states; validate stock and margin before proposing an offer. " + json.dumps(coverage[0]))
        return self._analysis_result(dict(spec, analysis_type="baskets"), "product.product", "Basket association analysis",
                                     rows, columns, labels, coverage, scope, export)

    def business_context(self):
        self._service()._assert_internal_user()
        roles = {"legacy.invoice": "Archived customer invoices: invoice_type=out_invoice; states open/paid; invoice_date. Old IDs are NOT current IDs.",
                 "legacy.invoice.line": "Archived purchase lines: invoice_id, partner_id, product_id, product_name/name/item_code, quantity. Legacy links may be missing.",
                 "legacy.invoice.customer.bridge": "Accepted customer mappings: invoice_id -> target_partner_id; strict/linked only; multiple targets are ambiguous.",
                 "account.move": "Current invoices: move_type=out_invoice and state=posted; invoice_date. Credit notes are out_refund.",
                 "account.move.line": "Current purchase lines: move_id, partner_id, product_id, quantity; filter parent type/state and positive quantities.",
                 "sale.order.line": "Confirmed order demand, not invoiced purchases. Do not add to invoices as revenue.",
                 "pos.order.line": "POS basket data. Check order state and overlap with invoices before combining.",
                 "product.product": "Current product variants. Inspect names/categories/codes to distinguish handsets from accessories; text mentions are insufficient.",
                 "res.partner": "Current customer identities: mobile/phone; individual Egyptian national ID in vat, company tax ID in vat. Never join databases by numeric ID or names."}
        available = []
        for name, meaning in roles.items():
            if name not in self.env:
                continue
            try:
                schema = self.describe_model(name)
                available.append({"model": name, "meaning": meaning,
                    "relationships": {key: val["relation"] for key, val in schema["fields"].items() if val.get("type") == "many2one"}})
            except (AccessError, ValidationError):
                continue
        return {"datasets": available, "guidance": "Use customer_sets for complete set differences, not subtraction of limited previews or sum of distinct counts. "
                "Use analyze_baskets for same-invoice bundles. Declare source/date overlap and unmapped identity coverage. "
                "For handset cohorts inspect product categories and exclude accessories/services; preserve user scope across follow-ups.",
                "financial_metrics": self.metric_definitions(), "shopify": self._shopify_context()}

"""Versioned financial definitions and source watermarks, with no business writes."""
from odoo import fields, models
from odoo.exceptions import AccessError, ValidationError


METRICS = {
    "invoice_net_revenue": {
        "label": "Net posted invoice revenue (ex tax)", "model": "account.move", "date_field": "invoice_date",
        "definition": "Sum amount_untaxed_signed once per posted customer invoice / credit note, by invoice date. "
            "Discounts are already included; credit notes reduce revenue. Company currency, no further FX conversion. "
            "Excludes drafts, cancelled documents, sales orders, separate POS totals, legacy archives and cash receipts. "
            "This is an invoice measure, not a reconciled general-ledger revenue figure.",
    },
    "ledger_gross_margin": {
        "label": "Posted ledger revenue and gross margin", "model": "account.move.line", "date_field": "date",
        "definition": "Accrual / posted journal items by accounting date. Revenue = minus balance on accounts of type income; "
            "COGS = balance on expense_direct_cost accounts; gross profit = revenue minus COGS; margin % = 100 * gross profit / revenue "
            "only when revenue is positive and COGS entries exist. Company currency, refunds/reversals keep their ledger signs. "
            "Other income, overheads, depreciation and tax accounts are outside this gross-margin definition. Not net profit. "
            "Relies on the chart of accounts and complete cost postings; missing/unclassified costs cannot prove profitability. "
            "Do not add invoice/POS/Shopify totals to this figure. No current product cost or hardcoded VAT divisor is used.",
    },
}


class CopilotMetrics(models.AbstractModel):
    _inherit = "ai.copilot.query"

    def metric_definitions(self):
        self._service()._assert_internal_user()
        return {"version": "2026-10-21", "metrics": METRICS,
                "guidance": "For ambiguous sales/profit ask which basis and period. Use financial_report for these canonical metrics. "
                    "Product-level historical contribution needs verified historical cost, UOM, tax and currency coverage; "
                    "current standard_price / total_cost is not historical COGS. Legacy revenue must remain separate until migration overlap is reconciled."}

    def _report_dates(self, spec):
        try:
            start, end = (fields.Date.to_date(spec[k]) for k in ("date_from", "date_to"))
        except (KeyError, ValueError, TypeError) as exc:
            raise ValidationError("Supply date_from and date_to as YYYY-MM-DD.") from exc
        if not start or not end or start > end or (end - start).days > 3660:
            raise ValidationError("Use an ordered reporting period of at most ten years.")
        return str(start), str(end)

    def financial_report(self, spec, export=False):
        self._service()._assert_internal_user()
        if not isinstance(spec, dict) or set(spec) - {"analysis_type", "model", "metric", "date_from", "date_to", "domain", "grain", "limit", "title", "chart"}:
            raise ValidationError("Invalid financial report definition.")
        metric = spec.get("metric")
        if metric not in METRICS:
            raise ValidationError("Choose a defined invoice or ledger financial metric.")
        definition = METRICS[metric]
        name, date_field = definition["model"], definition["date_field"]
        model, policy = self._analysis_model(name, export)
        start, end = self._report_dates(spec)
        grain = spec.get("grain", "total")
        if grain not in {"total", "day", "month"}:
            raise ValidationError("Group the report by total, day or month.")
        domain = self._analysis_domain(name, spec.get("domain", []), export)
        domain += [(date_field, ">=", start), (date_field, "<=", end)]
        groups = ["company_id"] + (["company_currency_id"] if name == "account.move.line" else []) + ([date_field + ":" + grain] if grain != "total" else [])
        required = {k.split(":")[0] for k in groups} | {date_field, "write_date", "state" if name == "account.move" else "parent_state"}
        required |= {"move_type", "amount_untaxed_signed"} if name == "account.move" else {"account_id", "move_id", "balance"}
        if not required <= self._readable_fields(name).keys():
            raise AccessError("Financial metric fields are unavailable under your field policy: %s" % ", ".join(sorted(required - self._readable_fields(name).keys())))
        domain.append(("state" if name == "account.move" else "parent_state", "=", "posted"))
        buckets = {}
        def aggregate(extra, measure):
            with self._budget():
                raw = model.read_group(domain + extra, [measure + ":sum", "write_date:max"], groups, lazy=False, limit=501)
            if len(raw) > 500:
                raise ValidationError("Financial report has too many groups; narrow the period or use monthly totals.")
            return raw
        def bucket(row):
            key = tuple(str(row.get(k, row.get(k.split(":")[0], False))) for k in groups)
            if key not in buckets:
                buckets[key] = {k: row.get(k, row.get(k.split(":")[0], False)) for k in groups}
            return buckets[key]
        watermarks = []
        coverage = []
        if metric == "invoice_net_revenue":
            extra = [("move_type", "in", ["out_invoice", "out_refund"])]
            for r in aggregate(extra, "amount_untaxed_signed"):
                bucket(r).update(net_revenue=r["amount_untaxed_signed"], document_count=r["__count"])
                watermarks.append(r.get("write_date"))
            companies, unused = self._model("res.company")
            if "currency_id" not in self._readable_fields("res.company"):
                raise AccessError("Company currency is unavailable under your field policy.")
            with self._budget():
                currencies = {r["id"]: r["currency_id"] for r in companies.search_read(
                    [("id", "in", [r["company_id"][0] for r in buckets.values()])], ["currency_id"], limit=500)}
            for r in buckets.values():
                if r["company_id"][0] not in currencies:
                    raise AccessError("Cannot establish the reporting company's currency under your permissions.")
                r["company_currency_id"] = currencies[r["company_id"][0]]
            columns = groups + ["company_currency_id", "net_revenue", "document_count"]
            coverage.append({"model": name, "domain": spec.get("domain", []), "date_from": start, "date_to": end,
                             "state": "posted", "move_type": ["out_invoice", "out_refund"]})
        else:
            account, unused = self._analysis_model("account.account", export)
            if "account_type" not in self._readable_fields("account.account"):
                raise AccessError("Account classifications are unavailable under your field policy.")
            # ORM subqueries preserve account rules without exposing / enumerating account IDs.
            for category, measure, count in [("income", "revenue", "revenue_entries"), ("expense_direct_cost", "cogs", "cogs_entries")]:
                for r in aggregate([("account_id", "in", account._search([("account_type", "=", category)]))], "balance"):
                    bucket(r).update({measure: -r["balance"] if category == "income" else r["balance"], count: r["__count"]})
                    watermarks.append(r.get("write_date"))
                coverage.append({"model": name, "domain": spec.get("domain", []), "date_from": start, "date_to": end,
                                 "parent_state": "posted", "account_type": category})
            for row in buckets.values():
                row.setdefault("revenue", 0.0)
                row.setdefault("cogs", 0.0)
                row.setdefault("revenue_entries", 0)
                row.setdefault("cogs_entries", 0)
                row["gross_profit"] = row["revenue"] - row["cogs"] if row["cogs_entries"] else None
                row["margin_pct"] = 100 * row["gross_profit"] / row["revenue"] if row["gross_profit"] is not None and row["revenue"] > 0 else None
                row["cost_coverage"] = "Posted direct-cost entries present; completeness not certified" if row["cogs_entries"] else "No visible direct-cost entries: margin unavailable"
            columns = groups + ["revenue", "cogs", "gross_profit", "margin_pct", "revenue_entries", "cogs_entries", "cost_coverage"]
        labels = {"company_id": "Company", "company_currency_id": "Company currency", date_field + ":" + grain: "Period",
                  "net_revenue": "Net invoice revenue (ex tax)", "document_count": "Invoices + credit notes",
                  "revenue": "Ledger revenue", "cogs": "Posted COGS", "gross_profit": "Ledger gross profit",
                  "margin_pct": "Gross margin %", "revenue_entries": "Revenue journal items", "cogs_entries": "COGS journal items", "cost_coverage": "Cost coverage"}
        spec = dict(spec, model=name, analysis_type="financial")
        result = self._analysis_result(spec, name, definition["label"], list(buckets.values()), columns, labels,
                                       coverage, definition["definition"], export)
        result["metric_definition"] = dict(definition, version="2026-10-21", metric=metric)
        result["provenance"]["metric_definition"] = result["metric_definition"]
        result["provenance"]["group_by"] = groups
        result["provenance"]["freshness"] = {"mode": "Saved Odoo records", "latest_visible_change_utc": str(max(filter(None, watermarks))) if any(watermarks) else None,
            "note": "Queried now does not certify upstream completeness. Latest saved modification is not a completed-sync timestamp."}
        # A chart may compare labeled company/date buckets, never mixed currencies.
        if len({str(r["company_currency_id"]) for r in result["rows"]}) == 1:
            result["chart_dimensions"] = ["company_id"] + ([date_field + ":" + grain] if grain != "total" else [])
            result["chart_measures"] = ["net_revenue" if metric == "invoice_net_revenue" else "revenue"]
            result["chart_unit"] = result["rows"][0]["company_currency_id"][1]
        return result

    def data_freshness(self, spec, export=False):
        self._service()._assert_internal_user()
        if not isinstance(spec, dict) or set(spec) - {"analysis_type", "model", "domain", "date_field", "limit", "title", "chart"}:
            raise ValidationError("Invalid freshness request.")
        name = spec.get("model")
        model, unused = self._analysis_model(name, export)
        meta = self._readable_fields(name)
        domain = self._analysis_domain(name, spec.get("domain", []), export)
        date_field = spec.get("date_field")
        dates = ["write_date"] if "write_date" in meta else []
        if date_field:
            if date_field not in meta or meta[date_field]["type"] not in {"date", "datetime"}:
                raise ValidationError("Choose a readable stored business date field.")
            dates = list(dict.fromkeys(dates + [date_field]))
        with self._budget():
            aggregates = [key + ":max" for key in dates]
            if date_field:
                aggregates.append("first_business_date:min(" + date_field + ")")
            data = model.read_group(domain, aggregates, [], lazy=False)[0]
        row = {"records": data.get("__count", 0), "latest_saved_change_utc": str(data.get("write_date") or "Unknown"),
               "earliest_business_date": str(data.get("first_business_date") or "Unknown") if date_field else "Not requested",
               "latest_business_date": str(data.get(date_field) or "Unknown") if date_field else "Not requested"}
        scope = "Saved records visible under your current permissions. Earliest/latest dates bound visible records; they do not certify gap-free transaction coverage or ingestion time. " \
                "Latest write_date is a modification watermark, not proof of a successful / complete upstream sync. No staleness SLA is configured."
        result = self._analysis_result(dict(spec, analysis_type="freshness"), name, "Saved data freshness", [row], list(row),
            {"records": "Matching records", "latest_saved_change_utc": "Latest saved modification (UTC)", "earliest_business_date": "Earliest business date", "latest_business_date": "Latest business date"},
            [{"model": name, "domain": spec.get("domain", []), "date_field": date_field}], scope, export)
        result["provenance"]["freshness"] = dict(row, mode="Saved Odoo records", note=scope)
        return result

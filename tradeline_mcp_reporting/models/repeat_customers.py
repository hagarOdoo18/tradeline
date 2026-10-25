"""Phone-identity repeat purchases using only the existing read-only MCP API.

Raw phone values are processed in memory and never returned, logged or saved.
"""
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
import hashlib
import hmac
import os
import re
import unicodedata
from zoneinfo import ZoneInfo

import phonenumbers


class RepeatReportError(ValueError):
    pass


GENERIC = {"cash", "cash customer", "walk in", "walk-in", "walkin", "customer", "guest",
           "anonymous", "public customer", "عميل نقدي", "عميل كاش", "كاش", "عميل عام"}


def normalize_phone(value):
    if not value:
        return None
    text = str(value).strip()
    # Do not silently select one of several phone numbers or drop an extension.
    converted = []
    for char in text:
        if char in "+ -().\u00a0":
            converted.append(char)
        else:
            try:
                converted.append(str(unicodedata.decimal(char)))
            except (TypeError, ValueError):
                return None
    text = "".join(converted)
    digits = re.sub(r"\D", "", text)
    if text.startswith("00"):
        text = "+" + text[2:]
    elif digits.startswith("20") and len(digits) == 12:
        text = "+" + digits
    # Conservative exclusions for obvious placeholder subscriber numbers.
    if re.search(r"(\d)\1{6,}$", digits) or any(digits.endswith(p) for p in
            ("0123456789", "1234567890", "9876543210", "0987654321")):
        return None
    try:
        parsed = phonenumbers.parse(text, "EG")
    except phonenumbers.NumberParseException:
        return None
    if parsed.extension or not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def identity(values, salt):
    normalized = {p for value in values if (p := normalize_phone(value))}
    if len(normalized) > 1:
        return None, "conflicting_phone_fields"
    if not normalized:
        return None, "missing_or_invalid_phone"
    phone = normalized.pop()
    return hmac.new(salt, phone.encode(), hashlib.sha256).digest(), None


def validate_spec(spec):
    if not isinstance(spec, dict) or set(spec) - {"years", "company_id", "as_of_date"}:
        raise RepeatReportError("Use years, company_id and optional as_of_date only.")
    years = spec.get("years")
    if not isinstance(years, list) or not 1 <= len(years) <= 5 or any(type(y) is not int or not 2000 <= y <= 2100 for y in years):
        raise RepeatReportError("Use one to five calendar years as integers.")
    company = spec.get("company_id")
    if type(company) is not int or company <= 0:
        raise RepeatReportError("Select a company ID from connection_info.")
    try:
        as_of = date.fromisoformat(spec.get("as_of_date") or datetime.now(ZoneInfo("Africa/Cairo")).date().isoformat())
    except (ValueError, TypeError):
        raise RepeatReportError("Use as_of_date YYYY-MM-DD.") from None
    if as_of > datetime.now(ZoneInfo("Africa/Cairo")).date() or min(years) > as_of.year:
        raise RepeatReportError("Future purchase periods cannot be reported.")
    return sorted(set(years)), company, as_of


def run_report(call, spec, progress=lambda value: None):
    years, company, as_of = validate_spec(spec)
    connection = call("connection_info", {})
    if not connection.get("read_only") or company not in {c["id"] for c in connection.get("company_scope", [])}:
        raise RepeatReportError("Requested company is outside the connected reporting scope.")
    salt = os.urandom(32)
    started = datetime.now(timezone.utc).isoformat()
    sources, evidence, pending, partners = [], [], [], set()
    phone_days = {y: defaultdict(set) for y in years}
    phone_events = {y: Counter() for y in years}
    quality = {y: Counter() for y in years}
    source_days = defaultdict(set)
    seen_events = set()
    schema = {}
    source_baselines = []
    total_scanned = 0

    def fields(model, required):
        if model not in schema:
            schema[model] = call("describe_model", {"model": model})["fields"]
        if not set(required) <= schema[model].keys():
            raise RepeatReportError("Required reporting fields are unavailable for " + model + ".")

    def checked_query(spec):
        result = call("query", {"spec": spec})
        if not isinstance(result, dict) or not isinstance(result.get("rows"), list) or result.get("cells_truncated"):
            raise RepeatReportError("Source response is unavailable or has truncated cells; no partial total accepted.")
        return result

    def add(year, day, event, key, reason, kind):
        if event in seen_events:
            quality[year]["duplicate_source_events_ignored"] += 1
            return
        seen_events.add(event)
        quality[year]["purchase_events"] += 1
        if not key:
            quality[year][reason] += 1
            return
        quality[year]["phone_matched_events"] += 1
        quality[year][kind] += 1
        phone_days[year][key].add(day)
        phone_events[year][key] += 1

    low, high = date(min(years), 1, 1), min(date(max(years), 12, 31), as_of)
    fields("res.partner", ["id", "name", "mobile", "phone"])
    definitions = [
        ("account.move", "invoice_date", ["id", "partner_id", "invoice_date"],
         [["state", "=", "posted"], ["move_type", "=", "out_invoice"], ["amount_total", ">", 0], ["payment_state", "!=", "reversed"]]),
        ("pos.order", "date_order", ["id", "partner_id", "date_order", "mobile"],
         [["state", "in", ["paid", "done", "invoiced"]], ["account_move", "=", False], ["amount_total", ">", 0]]),
        ("legacy.invoice", "invoice_date", ["id", "partner_id", "invoice_date", "source_partner_mobile", "source_db", "source_model", "source_id"],
         [["state", "in", ["open", "paid"]], ["invoice_type", "=", "out_invoice"], ["amount_total", ">", 0]]),
    ]
    for model, date_field, columns, base in definitions:
        fields(model, columns + ["company_id", "amount_total"])
        domain = [["company_id", "=", company]] + base
        if model == "pos.order":
            tz = ZoneInfo("Africa/Cairo")
            lower = datetime.combine(low, datetime.min.time(), tz).astimezone(timezone.utc).replace(tzinfo=None)
            from datetime import timedelta
            upper = datetime.combine(high + timedelta(days=1), datetime.min.time(), tz).astimezone(timezone.utc).replace(tzinfo=None)
            domain += [[date_field, ">=", lower.isoformat(sep=" ")], [date_field, "<", upper.isoformat(sep=" ")]]
        else:
            domain += [[date_field, ">=", low.isoformat()], [date_field, "<=", high.isoformat()]]
        top = checked_query({"model": model, "domain": domain, "fields": ["id"], "order": "id desc", "limit": 1})
        ceiling = top["rows"][0]["id"] if top["rows"] else 0
        bounded = domain + [["id", "<=", ceiling]]
        baseline = checked_query({"model": model, "domain": bounded, "aggregates": ["__count"], "limit": 1})
        expected = baseline["rows"][0]["__count"] if baseline["rows"] else 0
        if expected > 1000000:
            raise RepeatReportError("Source exceeds one million purchase documents; narrow the period.")
        evidence.append({"model": model, "definition": bounded, "records_at_start": expected, "citation": baseline.get("citation", {})})
        source_baselines.append((model, bounded, expected))
        marks = call("data_freshness", {"spec": {"model": model, "date_field": date_field, "domain": [["company_id", "=", company]] + base}})
        sources.append({"model": model, "watermarks": marks.get("rows", []), "citation": marks.get("citation", {})})
        last, count = 0, 0
        while True:
            result = checked_query({"model": model, "domain": bounded + [["id", ">", last]],
                                    "fields": columns, "order": "id asc", "limit": 500})
            rows = result["rows"]
            row_ids = [r.get("id") for r in rows]
            if any(type(v) is not int or v <= last or v > ceiling for v in row_ids) or row_ids != sorted(set(row_ids)):
                raise RepeatReportError("Source pagination could not be verified.")
            for row in rows:
                raw = row[date_field]
                if model == "pos.order":
                    day = datetime.fromisoformat(raw).replace(tzinfo=timezone.utc).astimezone(ZoneInfo("Africa/Cairo")).date()
                else:
                    day = date.fromisoformat(raw)
                if day.year not in years:
                    continue
                source_days[model].add(day)
                direct = row.get("source_partner_mobile") if model == "legacy.invoice" else row.get("mobile")
                key, reason = identity([direct], salt)
                event = (model, row["id"])
                if model == "legacy.invoice" and row.get("source_id"):
                    event = (model, row.get("source_db"), row.get("source_model"), row["source_id"])
                if key:
                    add(day.year, day, event, key, None, "document_phone_events")
                elif row.get("partner_id"):
                    partner = row["partner_id"][0]
                    partners.add(partner)
                    pending.append((day.year, day, event, partner))
                else:
                    add(day.year, day, event, None, reason, "")
            count += len(rows)
            total_scanned += len(rows)
            progress({"phase": "purchase_documents", "source": model, "scanned_source": count, "source_records": expected, "total_documents_scanned": total_scanned})
            if total_scanned > 1500000:
                raise RepeatReportError("Combined document budget exceeded; narrow the period.")
            if not result.get("truncated"):
                break
            if not rows:
                raise RepeatReportError("Incomplete source pagination; no partial count accepted.")
            last = row_ids[-1]
        if count != expected:
            raise RepeatReportError("Purchase records changed during the scan; no complete count can be certified. Retry.")

    if source_days["account.move"] & source_days["legacy.invoice"]:
        raise RepeatReportError("Current and legacy invoice periods overlap. Reconcile source duplication before combining customer frequencies.")
    fields("res.partner", ["id", "name", "mobile", "phone"])
    if len(partners) > 250000:
        raise RepeatReportError("Contact lookup exceeds 250,000 records; narrow the period.")
    contact_keys, contact_phone_owners = {}, defaultdict(set)
    ordered = sorted(partners)
    for start in range(0, len(ordered), 500):
        batch = ordered[start:start + 500]
        result = checked_query({"model": "res.partner", "domain": [["id", "in", batch]],
            "fields": ["id", "name", "mobile", "phone"], "include_archived": True, "limit": 500})
        if result.get("truncated"):
            raise RepeatReportError("Contact result is truncated.")
        for row in result["rows"]:
            if str(row.get("name") or "").strip().casefold() in GENERIC:
                contact_keys[row["id"]] = (None, "generic_walk_in_contact")
            else:
                contact_keys[row["id"]] = identity([row.get("mobile"), row.get("phone")], salt)
                if contact_keys[row["id"]][0]:
                    contact_phone_owners[contact_keys[row["id"]][0]].add(row["id"])
        progress({"phase": "contact_identity", "contacts_scanned": min(start + 500, len(ordered)), "contacts_needed": len(ordered), "total_documents_scanned": total_scanned})
    for year, day, event, partner in pending:
        key, reason = contact_keys.get(partner, (None, "contact_unavailable_under_record_rules"))
        add(year, day, event, key, reason, "linked_contact_phone_events")
    # Detect additions/removals from the bounded source populations. This is
    # still a live multi-request read, not a historic database snapshot.
    for model, domain, expected in source_baselines:
        result = checked_query({"model": model, "domain": domain, "aggregates": ["__count"], "limit": 1})
        final = result["rows"][0]["__count"] if result["rows"] else 0
        if final != expected:
            raise RepeatReportError("Source population changed during the scan; retry instead of using partial counts.")
    annual = []
    for year in years:
        keys = phone_days[year]
        q = quality[year]
        annual.append({"year": year, "period_from": date(year, 1, 1).isoformat(),
            "period_to": min(date(year, 12, 31), as_of).isoformat(),
            "distinct_phone_identities": len(keys),
            "repeat_phone_identities_two_purchase_dates": sum(len(days) >= 2 for days in keys.values()),
            "repeat_phone_identities_two_purchase_documents": sum(n >= 2 for n in phone_events[year].values()),
            "purchase_events": q["purchase_events"], "phone_matched_events": q["phone_matched_events"],
            "unmatched_events": q["purchase_events"] - q["phone_matched_events"],
            "matching_event_coverage_percent": round(100 * q["phone_matched_events"] / q["purchase_events"], 2) if q["purchase_events"] else None,
            "quality_counts": dict(q)})
    both = len(set.intersection(*(set(phone_days[y]) for y in years))) if len(years) > 1 else None
    return {"company": next(c for c in connection["company_scope"] if c["id"] == company),
        "identity": "Normalized valid phone number, not a verified unique person. No name-based or cross-database numeric-ID matching.",
        "definition": "Primary repeat metric: one phone identity on at least two distinct Cairo purchase dates within each calendar year. Secondary metric: at least two separate positive purchase documents. Current posted invoices excluding payment_state=reversed, completed positive POS without invoice links, and posted legacy customer invoices. Credit notes are not new purchases. Legacy refund-to-original reconciliation is not available in this calculation.",
        "annual": annual, "phone_identities_present_in_all_selected_years": both,
        "phones_shared_across_multiple_contact_records": sum(len(v) > 1 for v in contact_phone_owners.values()),
        "sources": sources, "evidence": evidence, "complete_scan": True,
        "started_at_utc": started, "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "limitations": ["Phone identities may be shared by a household or business, recycled or changed. They are not certified distinct people.",
            "Missing/invalid/conflicting phones, generic contacts and unreadable contacts are excluded and disclosed, not silently matched.",
            "No B2B, online or retail-channel exclusions were inferred; this is the selected company across available channels.",
            "2026 is year to date. Date watermarks do not certify gap-free imported history.",
            "Reads span multiple requests. Bounded IDs and start/end population checks do not create a historical point-in-time snapshot.",
            "Sales orders and Shopify orders are not added to invoice purchases. Original Odoo 12 and live Shopify are not queried."],
        "raw_identifiers_returned": False}

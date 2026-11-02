"""In-request identity comparison. Identifiers never leave this server-side helper."""
import datetime
import time
import unicodedata
from collections import defaultdict

from odoo import models
from odoo.exceptions import AccessError, ValidationError


def digits(value):
    result = []
    for char in str(value or "").strip():
        if char in " +-().\u00a0":
            continue
        try:
            result.append(str(unicodedata.decimal(char)))
        except (TypeError, ValueError):
            return ""
    return "".join(result)


def phone_key(value):
    raw = str(value or "").strip()
    number = digits(raw)
    if number.startswith("0020"):
        number = number[4:]
    elif number.startswith("20") and len(number) == 12:
        number = number[2:]
    if len(number) == 10 and number[:2] in {"10", "11", "12", "15"}:
        return "20" + number
    if len(number) == 11 and number[:3] in {"010", "011", "012", "015"}:
        return "2" + number
    # Other countries require an explicit international prefix; never infer a country.
    if raw.startswith(("+", "00")):
        number = number[2:] if number.startswith("00") else number
        if 8 <= len(number) <= 15 and not number.startswith("0") and len(set(number)) > 2:
            return number
    return ""


def national_key(value):
    number = digits(value)
    if len(number) != 14 or number[0] not in "23":
        return ""
    try:
        born = datetime.date((1700 + int(number[0]) * 100) + int(number[1:3]), int(number[3:5]), int(number[5:7]))
    except ValueError:
        return ""
    return number if born <= datetime.date.today() else ""


def identity_match(nationals, phones, index):
    """Unique exact normalized matches only. A conflicting signal vetoes a match."""
    national_sets = [index["national"].get(key, set()) for key in set(nationals) if key]
    phone_sets = [index["phone"].get(key, set()) for key in set(phones) if key]
    sets = national_sets + phone_sets
    if any(len(targets) > 1 for targets in sets):
        return False, "ambiguous"
    candidates = set().union(*sets) if sets else set()
    if len(candidates) > 1:
        return False, "conflicting"
    if national_sets and any(not targets for targets in national_sets):
        return False, "unmatched_national_id"
    if len(candidates) == 1:
        return candidates.pop(), "national_id" if national_sets else "phone"
    return False, "unmatched" if sets else "missing_identifiers"


class CopilotIdentity(models.AbstractModel):
    _inherit = "ai.copilot.query"

    IDENTITY_CONTACT_LIMIT = 200000

    def _identity_read_fields(self, name, candidates):
        self._analysis_model(name)
        readable = self._readable_fields(name)
        return [field for field in candidates if field in readable]

    def _identity_index(self, export, deadline):
        model, unused_policy = self._analysis_model("res.partner", export)
        allowed = self._identity_read_fields("res.partner", ["mobile", "phone", "vat", "is_company"])
        if "is_company" not in allowed or not ({"mobile", "phone", "vat"} & set(allowed)):
            raise AccessError("Customer identifier fields are unavailable under the data policy.")
        # Include archived contacts: an archived duplicate still makes an identifier ambiguous.
        model = model.with_context(active_test=False)
        index = {"phone": defaultdict(set), "national": defaultdict(set)}
        last_id, scanned = 0, 0
        while True:
            if time.monotonic() > deadline:
                raise ValidationError("Identity lookup time budget reached. No partial identity matches were accepted.")
            remaining = self.IDENTITY_CONTACT_LIMIT - scanned
            with self._budget():
                rows = model.search_read([("id", ">", last_id)], ["id"] + allowed,
                                         order="id", limit=min(5000, remaining + 1))
            if len(rows) > remaining:
                raise ValidationError("Identity lookup exceeds 200,000 visible contacts. No partial identity matches were accepted.")
            for row in rows:
                for field in ("mobile", "phone"):
                    key = phone_key(row.get(field))
                    if key:
                        index["phone"][key].add(row["id"])
                # This installation explicitly stores individuals' Egyptian national IDs in VAT.
                key = national_key(row.get("vat")) if not row.get("is_company") else ""
                if key:
                    index["national"][key].add(row["id"])
            scanned += len(rows)
            if len(rows) < min(5000, remaining + 1):
                return index
            last_id = rows[-1]["id"]

    def _legacy_identifier_matches(self, line_model, domain, counts, blocked, export, deadline, cache,
                                   header_name="legacy.invoice", parent_field="invoice_id"):
        header, unused_policy = self._analysis_model(header_name, export)
        header_fields = self._identity_read_fields(header._name, ["source_partner_mobile", "source_partner_phone", "source_partner_national_id", "source_partner_tax_id", "source_partner_type"])
        line_fields = self._identity_read_fields(line_model._name, ["customer_mobile", "customer_phone"])
        if not header_fields and not line_fields:
            raise AccessError("Legacy customer identifier fields are unavailable under the data policy.")
        if "index" not in cache:
            cache["index"] = self._identity_index(export, deadline)
        methods, mappings = defaultdict(int), {}
        ids = [invoice_id for invoice_id in counts if invoice_id not in blocked]
        for start in range(0, len(ids), 500):
            if time.monotonic() > deadline:
                raise ValidationError("Identity analysis time budget reached; narrow the source dates or products.")
            batch = ids[start:start+500]
            with self._budget():
                headers = header.search_read([("id", "in", batch)], ["id"] + header_fields, limit=500)
            evidence = {row["id"]: {"national": set(), "phone": set()} for row in headers}
            for row in headers:
                item = evidence[row["id"]]
                item["phone"].add(phone_key(row.get("source_partner_mobile")))
                item["phone"].add(phone_key(row.get("source_partner_phone")))
                item["national"].add(national_key(row.get("source_partner_national_id")))
                if str(row.get("source_partner_type") or "").lower() not in {"company", "business"}:
                    item["national"].add(national_key(row.get("source_partner_tax_id")))
            # Group line identifiers within the exact source scope; raw identifiers are never returned.
            if line_fields:
                groups = self._analysis_groups(line_model, domain + [(parent_field, "in", batch)], [parent_field] + line_fields, deadline)
                for row in groups:
                    invoice_id = row[parent_field][0] if row.get(parent_field) else False
                    if invoice_id in evidence:
                        for field in line_fields:
                            evidence[invoice_id]["phone"].add(phone_key(row.get(field)))
            for invoice_id, item in evidence.items():
                target, method = identity_match(item["national"], item["phone"], cache["index"])
                methods[method] += counts[invoice_id]
                if target:
                    mappings[invoice_id] = target
        return mappings, dict(methods)

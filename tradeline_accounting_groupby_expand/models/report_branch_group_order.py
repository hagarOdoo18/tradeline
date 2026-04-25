from datetime import datetime, time
import re

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models
from odoo.osv import expression


_NUMERIC_FIELD_TYPES = {"integer", "float", "monetary"}
SUPPORTED_TEXT_OPERATORS = {"=", "ilike", "like", "=ilike", "=like"}
NAME_SEARCH_TEXT_OPERATORS = {"ilike", "like", "=ilike", "=like"}
POSITIVE_TEXT_OPERATORS = {"=", "ilike", "like", "=ilike", "=like"}
NEGATIVE_TEXT_OPERATORS = {"!=", "<>", "not ilike", "not like"}
TOKEN_BOUNDARY_RE = r"(?<![a-z0-9])%s(?![a-z0-9])"


def _ordered_unique_ids(values):
    ordered = []
    seen = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def _split_search_tokens(value):
    tokens = [token.lower() for token in re.split(r"[\s,;/|()\-]+", (value or "").strip()) if token]
    return [token for token in tokens if len(token) >= 2 or token.isdigit()]


def _token_present(text, token):
    haystack = (text or "").lower()
    if not haystack:
        return False
    if re.fullmatch(r"[a-z0-9]+", token):
        pattern = TOKEN_BOUNDARY_RE % re.escape(token)
        return bool(re.search(pattern, haystack))
    return token in haystack


def _product_broad_text(product):
    attrs_text = " ".join(product.product_template_variant_value_ids.mapped("name"))
    return " ".join(
        filter(
            None,
            [
                product.display_name,
                product.name,
                product.product_tmpl_id.name,
                attrs_text,
                product.barcode,
                product.default_code,
            ],
        )
    ).lower()


def _all_tokens_match_strict(text, tokens):
    return all(_token_present(text, token) for token in tokens)


def _normalize_phrase_text(value):
    normalized = re.sub(r"[^\w]+", " ", (value or "").lower(), flags=re.UNICODE)
    return re.sub(r"\s+", " ", normalized).strip()


def _phrase_present(text, query):
    normalized_query = _normalize_phrase_text(query)
    if not normalized_query:
        return False
    normalized_text = _normalize_phrase_text(text)
    return normalized_query in normalized_text


def _all_tokens_match_ordered_relaxed(text, tokens):
    haystack = (text or "").lower()
    if not haystack:
        return False
    start_at = 0
    for token in tokens:
        idx = haystack.find(token, start_at)
        if idx == -1:
            return False
        start_at = idx + len(token)
    return True


def _all_tokens_match_relaxed(text, tokens):
    haystack = (text or "").lower()
    return all(token in haystack for token in tokens)


def _search_invoiced_product_ids_by_item_code(model, value, operator="ilike", limit=5000):
    value = (value or "").strip()
    if not value:
        return []

    lines = model.env["account.move.line"].search(
        [
            ("product_id", "!=", False),
            ("item_code", operator, value),
            ("move_id.state", "=", "posted"),
            ("move_id.move_type", "in", ("out_invoice", "out_refund", "out_receipt")),
        ],
        limit=limit,
    )
    return lines.mapped("product_id").ids


def _search_product_ids_by_specific_text(model, value, operator="ilike", limit=5000):
    value = (value or "").strip()
    if not value:
        return []

    tokens = _split_search_tokens(value)
    if len(tokens) <= 1:
        effective_operator = operator if operator in SUPPORTED_TEXT_OPERATORS else "ilike"
        products = model.env["product.product"].search(
            [("display_name", effective_operator, value)],
            limit=limit,
        )
    else:
        token_domain = expression.AND([[("display_name", "ilike", token)] for token in tokens])
        products = model.env["product.product"].search(token_domain, limit=limit).filtered(
            lambda product: all(_token_present((product.display_name or "").lower(), token) for token in tokens)
        )
    return products.ids


def _search_product_ids_by_text(model, value, operator="ilike", limit=5000):
    value = (value or "").strip()
    if not value:
        return []

    effective_operator = operator if operator in NAME_SEARCH_TEXT_OPERATORS else "ilike"
    # Reuse product.product name_search so Invoice Analysis and Apple Stock
    # follow the same broad product matching/ranking behavior as Product Variants.
    product_matches = model.env["product.product"].name_search(
        name=value,
        args=[],
        operator=effective_operator,
        limit=limit,
    )
    product_ids = _ordered_unique_ids([product_id for product_id, _name in product_matches])

    tokens = _split_search_tokens(value)
    if len(tokens) <= 1:
        return product_ids

    products_by_id = {product.id: product for product in model.env["product.product"].browse(product_ids).exists()}
    phrase_ids = [
        product_id
        for product_id in product_ids
        if product_id in products_by_id
        and _phrase_present(_product_broad_text(products_by_id[product_id]), value)
    ]
    if phrase_ids:
        return phrase_ids

    strict_ids = [
        product_id
        for product_id in product_ids
        if product_id in products_by_id
        and _all_tokens_match_strict(_product_broad_text(products_by_id[product_id]), tokens)
    ]
    if strict_ids:
        return strict_ids

    ordered_relaxed_ids = [
        product_id
        for product_id in product_ids
        if product_id in products_by_id
        and _all_tokens_match_ordered_relaxed(_product_broad_text(products_by_id[product_id]), tokens)
    ]
    if ordered_relaxed_ids:
        return ordered_relaxed_ids

    relaxed_ids = [
        product_id
        for product_id in product_ids
        if product_id in products_by_id
        and _all_tokens_match_relaxed(_product_broad_text(products_by_id[product_id]), tokens)
    ]
    return relaxed_ids or product_ids


def _search_product_ids_by_item_code(model, value, operator="ilike", limit=5000):
    value = (value or "").strip()
    if not value:
        return []

    products = model.env["product.product"].search(
        ["|", ("barcode", operator, value), ("default_code", operator, value)],
        limit=limit,
    )
    return products.ids


def _rewrite_product_id_text_domain(model, domain):
    if not domain:
        return domain

    def _rewrite_leaf(term):
        if not isinstance(term, (list, tuple)) or len(term) < 3:
            return term

        field_name, operator, value = term[0], term[1], term[2]
        if field_name != "product_id":
            return term
        if operator not in SUPPORTED_TEXT_OPERATORS:
            return term
        if not isinstance(value, str):
            return term

        product_ids = _search_product_ids_by_text(model, value, operator=operator, limit=5000)
        if not product_ids:
            return ("id", "=", 0)
        return ("product_id", "in", product_ids)

    def _rewrite(node):
        rewritten_leaf = _rewrite_leaf(node)
        if rewritten_leaf is not node:
            return rewritten_leaf

        if isinstance(node, list):
            return [_rewrite(item) for item in node]
        return node

    return _rewrite(domain)


def _has_legacy_time_keys(ctx):
    return bool(
        ctx.get("tradeline_time_range")
        or ctx.get("tradeline_time_based_on")
        or (ctx.get("tradeline_time_compare") and ctx.get("tradeline_time_compare") != "none")
    )


def _is_time_engine_enabled(model):
    ctx = model.env.context
    if ctx.get("tradeline_time_ranges_native") and not ctx.get("tradeline_time_legacy_mode"):
        return _has_legacy_time_keys(ctx)
    return bool(
        ctx.get("tradeline_time_legacy_mode")
        or ctx.get("tradeline_time_ranges_enabled")
        or ctx.get("tradeline_time_engine_enabled")
        or _has_legacy_time_keys(ctx)
    )


def _is_date_field(model, field_name):
    field = model._fields.get(field_name)
    return bool(field and field.type in {"date", "datetime"})


def _resolve_based_on_field(model, default_field):
    based_on = model.env.context.get("tradeline_time_based_on")
    if _is_date_field(model, based_on):
        return based_on
    if _is_date_field(model, default_field):
        return default_field
    for field_name, field in model._fields.items():
        if field.type in {"date", "datetime"}:
            return field_name
    return None


def _range_key(model):
    return model.env.context.get("tradeline_time_range")


def _safe_to_date(value):
    try:
        return fields.Date.to_date(value)
    except Exception:
        return None


def _month_start(day):
    return day + relativedelta(day=1)


def _quarter_start(day):
    first_month = ((day.month - 1) // 3) * 3 + 1
    return day + relativedelta(month=first_month, day=1)


def _year_start(day):
    return day + relativedelta(month=1, day=1)


def _compute_interval(model, range_key):
    if not range_key:
        return None

    today = fields.Date.context_today(model)
    tomorrow = today + relativedelta(days=1)

    if range_key == "last_7_days":
        return (today - relativedelta(days=6), tomorrow)
    if range_key == "last_30_days":
        return (today - relativedelta(days=29), tomorrow)
    if range_key == "last_365_days":
        return (today - relativedelta(days=364), tomorrow)
    if range_key == "today":
        return (today, tomorrow)
    if range_key == "this_week":
        week_start = today - relativedelta(days=today.weekday())
        return (week_start, tomorrow)
    if range_key == "this_month":
        return (_month_start(today), tomorrow)
    if range_key == "this_quarter":
        return (_quarter_start(today), tomorrow)
    if range_key == "this_year":
        return (_year_start(today), tomorrow)
    if range_key == "yesterday":
        return (today - relativedelta(days=1), today)
    if range_key == "last_week":
        this_week_start = today - relativedelta(days=today.weekday())
        return (this_week_start - relativedelta(days=7), this_week_start)
    if range_key == "last_month":
        this_month_start = _month_start(today)
        return (this_month_start - relativedelta(months=1), this_month_start)
    if range_key == "last_quarter":
        this_quarter_start = _quarter_start(today)
        return (this_quarter_start - relativedelta(months=3), this_quarter_start)
    if range_key == "last_year":
        this_year_start = _year_start(today)
        return (this_year_start - relativedelta(years=1), this_year_start)
    if range_key == "custom":
        start = _safe_to_date(model.env.context.get("tradeline_time_date_from"))
        end = _safe_to_date(model.env.context.get("tradeline_time_date_to"))
        if not start or not end:
            return None
        if end < start:
            start, end = end, start
        return (start, end + relativedelta(days=1))
    return None


def _compare_mode(model):
    return model.env.context.get("tradeline_time_compare") or "none"


def _compare_interval(model, current_interval):
    mode = _compare_mode(model)
    if mode in (None, False, "none"):
        return (None, mode)

    if not current_interval:
        return (None, mode)
    start, end = current_interval

    if mode == "previous_period":
        length_days = max((end - start).days, 1)
        return ((start - relativedelta(days=length_days), start), mode)
    if mode == "previous_year":
        return ((start - relativedelta(years=1), end - relativedelta(years=1)), mode)
    if mode == "custom":
        cmp_start = _safe_to_date(model.env.context.get("tradeline_time_compare_from"))
        cmp_end = _safe_to_date(model.env.context.get("tradeline_time_compare_to"))
        if not cmp_start or not cmp_end:
            return (None, mode)
        if cmp_end < cmp_start:
            cmp_start, cmp_end = cmp_end, cmp_start
        return ((cmp_start, cmp_end + relativedelta(days=1)), mode)
    return (None, mode)


def _interval_domain(model, field_name, interval):
    if not interval:
        return []

    start, end = interval
    field = model._fields.get(field_name)
    if not field:
        return []

    if field.type == "datetime":
        start_dt = datetime.combine(start, time.min)
        end_dt = datetime.combine(end, time.min)
        return [
            (field_name, ">=", fields.Datetime.to_string(start_dt)),
            (field_name, "<", fields.Datetime.to_string(end_dt)),
        ]
    return [
        (field_name, ">=", fields.Date.to_string(start)),
        (field_name, "<", fields.Date.to_string(end)),
    ]


def _apply_time_domain(model, base_domain, default_field):
    if not _is_time_engine_enabled(model):
        return (base_domain, None, None)

    based_on = _resolve_based_on_field(model, default_field)
    if not based_on:
        return (base_domain, None, None)

    interval = _compute_interval(model, _range_key(model))
    if not interval:
        return (base_domain, based_on, None)

    current_domain = expression.AND([base_domain or [], _interval_domain(model, based_on, interval)])
    return (current_domain, based_on, interval)


def _measure_field_names(model, requested_fields):
    fields_seen = []
    for requested in requested_fields or []:
        if not isinstance(requested, str):
            continue
        field_name = requested.split(":", 1)[0]
        field = model._fields.get(field_name)
        if not field or field.type not in _NUMERIC_FIELD_TYPES:
            continue
        if field_name not in fields_seen:
            fields_seen.append(field_name)
    return fields_seen


def _signature_value(value):
    if isinstance(value, (list, tuple)):
        if not value:
            return False
        return value[0]
    return value


def _group_signature(row, groupby):
    tokens = _groupby_list(groupby)
    signature = []
    for token in tokens:
        normalized = _normalize_groupby_token(token)
        value = row.get(token, row.get(normalized))
        signature.append(_signature_value(value))
    return tuple(signature)


def _attach_compare_payload(model, current_results, compare_results, requested_fields, groupby, compare_mode):
    if not current_results or not compare_results:
        return current_results

    numeric_fields = _measure_field_names(model, requested_fields)
    if not numeric_fields:
        return current_results

    compare_map = {}
    for compare_row in compare_results:
        compare_map[_group_signature(compare_row, groupby)] = compare_row

    output_mode = model.env.context.get("tradeline_time_compare_output") or "current"

    for row in current_results:
        counterpart = compare_map.get(_group_signature(row, groupby), {})
        payload = {}
        for field_name in numeric_fields:
            current_value = row.get(field_name) or 0.0
            previous_value = counterpart.get(field_name) or 0.0
            if not isinstance(current_value, (int, float)):
                current_value = 0.0
            if not isinstance(previous_value, (int, float)):
                previous_value = 0.0
            delta = current_value - previous_value
            delta_pct = (delta / previous_value * 100.0) if previous_value else False

            row[f"{field_name}__previous"] = previous_value
            row[f"{field_name}__delta"] = delta
            row[f"{field_name}__delta_pct"] = delta_pct
            payload[field_name] = {
                "current": current_value,
                "previous": previous_value,
                "delta": delta,
                "delta_pct": delta_pct,
            }
            if output_mode == "delta":
                row[field_name] = delta
            elif output_mode == "previous":
                row[field_name] = previous_value
            elif output_mode == "delta_pct":
                row[field_name] = delta_pct or 0.0

        row["tradeline_compare_mode"] = compare_mode
        row["tradeline_compare"] = payload
    return current_results


def _normalize_groupby_token(token):
    if not isinstance(token, str):
        return ""
    return token.split(":", 1)[0]


def _is_current_group_branch(groupby):
    if isinstance(groupby, str):
        current = _normalize_groupby_token(groupby)
    elif isinstance(groupby, (list, tuple)) and groupby:
        current = _normalize_groupby_token(groupby[0])
    else:
        current = ""
    return current == "branch_id"


def _groupby_list(groupby):
    if isinstance(groupby, str):
        return [groupby]
    if isinstance(groupby, (list, tuple)):
        return list(groupby)
    return []


def _value_sort_key(value):
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return (0, str(value[1] or "").casefold())
    if isinstance(value, (list, tuple)) and value:
        return (0, str(value[0]).casefold())
    if value in (False, None):
        return (1, "")
    return (0, str(value).casefold())


def _branch_sort_key_from_value(branch_value):
    if isinstance(branch_value, (list, tuple)) and len(branch_value) >= 2:
        branch_name = branch_value[1] or ""
        return (0, str(branch_name).casefold())
    if isinstance(branch_value, str):
        return (0, branch_value.casefold())
    return (1, "")


def _row_group_value_key(row, token):
    normalized_token = _normalize_groupby_token(token)
    if token in row:
        return _value_sort_key(row.get(token))
    if normalized_token in row:
        return _value_sort_key(row.get(normalized_token))
    return (1, "")


def _sort_groups_by_branch(results, groupby):
    group_tokens = _groupby_list(groupby)
    normalized_tokens = [_normalize_groupby_token(token) for token in group_tokens]
    if "branch_id" not in normalized_tokens:
        return results

    branch_index = normalized_tokens.index("branch_id")
    parent_tokens = group_tokens[:branch_index]

    def _key(row):
        parent_key = tuple(_row_group_value_key(row, token) for token in parent_tokens)
        branch_key = _branch_sort_key_from_value(row.get("branch_id"))
        return parent_key + (branch_key,)

    return sorted(results, key=_key)


class AccountInvoiceReport(models.Model):
    _inherit = "account.invoice.report"

    product_search_text = fields.Char(
        string="Product",
        compute="_compute_product_search_text",
        search="_search_product_search_text",
        store=False,
    )
    item_code_search_text = fields.Char(
        string="Item Code",
        compute="_compute_item_code_search_text",
        search="_search_item_code_search_text",
        store=False,
    )

    @api.depends("product_id")
    def _compute_product_search_text(self):
        for rec in self:
            rec.product_search_text = rec.product_id.display_name or ""

    @api.depends("product_id")
    def _compute_item_code_search_text(self):
        for rec in self:
            rec.item_code_search_text = rec.product_id.barcode or rec.product_id.default_code or ""

    def _search_product_search_text(self, operator, value):
        value = (value or "").strip()
        if not value:
            return []
        product_ids = _search_product_ids_by_specific_text(self, value, operator=operator, limit=5000)
        if not product_ids:
            return [("id", "=", 0)]
        return [("product_id", "in", product_ids)]

    def _search_item_code_search_text(self, operator, value):
        value = (value or "").strip()
        if not value:
            return []

        if operator in POSITIVE_TEXT_OPERATORS:
            product_ids = _search_invoiced_product_ids_by_item_code(self, value, operator=operator, limit=5000)
            product_ids += _search_product_ids_by_item_code(self, value, operator=operator, limit=5000)
            product_ids = _ordered_unique_ids(product_ids)
            if not product_ids:
                return [("id", "=", 0)]
            return [("product_id", "in", product_ids)]

        if operator in NEGATIVE_TEXT_OPERATORS:
            positive_operator = {
                "!=": "=",
                "<>": "=",
                "not ilike": "ilike",
                "not like": "like",
            }[operator]
            product_ids = _search_invoiced_product_ids_by_item_code(self, value, operator=positive_operator, limit=5000)
            product_ids += _search_product_ids_by_item_code(self, value, operator=positive_operator, limit=5000)
            product_ids = _ordered_unique_ids(product_ids)
            if not product_ids:
                return [("id", "!=", 0)]
            return [("product_id", "not in", product_ids)]

        return []

    @api.model
    def _rewrite_product_id_text_domain(self, domain):
        return _rewrite_product_id_text_domain(self, domain)

    @api.model
    def _search(self, domain, offset=0, limit=None, order=None, *args, **kwargs):
        domain = self._rewrite_product_id_text_domain(domain)
        domain, _, _ = _apply_time_domain(self, domain, default_field="invoice_date")
        return super()._search(
            domain,
            offset=offset,
            limit=limit,
            order=order,
            *args,
            **kwargs,
        )

    @api.model
    def read_group(
        self,
        domain,
        fields,
        groupby,
        offset=0,
        limit=None,
        orderby=False,
        lazy=True,
    ):
        domain = self._rewrite_product_id_text_domain(domain)
        scoped_domain, based_on, current_interval = _apply_time_domain(
            self,
            domain,
            default_field="invoice_date",
        )
        results = super().read_group(
            scoped_domain,
            fields,
            groupby,
            offset=offset,
            limit=limit,
            orderby=orderby,
            lazy=lazy,
        )
        compare_interval, compare_mode = _compare_interval(self, current_interval)
        if compare_interval and based_on:
            compare_domain = expression.AND([domain or [], _interval_domain(self, based_on, compare_interval)])
            compare_results = super().read_group(
                compare_domain,
                fields,
                groupby,
                offset=offset,
                limit=limit,
                orderby=orderby,
                lazy=lazy,
            )
            results = _attach_compare_payload(self, results, compare_results, fields, groupby, compare_mode)

        if _is_current_group_branch(groupby):
            return sorted(results, key=lambda row: _branch_sort_key_from_value(row.get("branch_id")))
        return _sort_groups_by_branch(results, groupby)


def _enable_branch_alpha_on_model(model):
    return bool(model.env.context.get("tradeline_branch_group_alpha"))


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    product_search_text = fields.Char(
        string="Product",
        compute="_compute_product_search_text",
        search="_search_product_search_text",
        store=False,
    )

    @api.depends("product_id")
    def _compute_product_search_text(self):
        for rec in self:
            rec.product_search_text = rec.product_id.display_name or ""

    def _search_product_search_text(self, operator, value):
        value = (value or "").strip()
        if not value:
            return []
        product_ids = _search_product_ids_by_text(self, value, operator=operator, limit=5000)
        if not product_ids:
            return [("id", "=", 0)]
        return [("product_id", "in", product_ids)]

    @api.model
    def _rewrite_product_id_text_domain(self, domain):
        return _rewrite_product_id_text_domain(self, domain)

    @api.model
    def search(self, domain, offset=0, limit=None, order=None):
        domain = self._rewrite_product_id_text_domain(domain)
        domain, _, _ = _apply_time_domain(self, domain, default_field="date")
        return super().search(domain, offset=offset, limit=limit, order=order)

    @api.model
    def read_group(
        self,
        domain,
        fields,
        groupby,
        offset=0,
        limit=None,
        orderby=False,
        lazy=True,
    ):
        domain = self._rewrite_product_id_text_domain(domain)
        scoped_domain, based_on, current_interval = _apply_time_domain(
            self,
            domain,
            default_field="date",
        )
        results = super().read_group(
            scoped_domain,
            fields,
            groupby,
            offset=offset,
            limit=limit,
            orderby=orderby,
            lazy=lazy,
        )
        compare_interval, compare_mode = _compare_interval(self, current_interval)
        if compare_interval and based_on:
            compare_domain = expression.AND([domain or [], _interval_domain(self, based_on, compare_interval)])
            compare_results = super().read_group(
                compare_domain,
                fields,
                groupby,
                offset=offset,
                limit=limit,
                orderby=orderby,
                lazy=lazy,
            )
            results = _attach_compare_payload(self, results, compare_results, fields, groupby, compare_mode)

        if not _enable_branch_alpha_on_model(self):
            return results

        if _is_current_group_branch(groupby):
            return sorted(results, key=lambda row: _branch_sort_key_from_value(row.get("branch_id")))
        return _sort_groups_by_branch(results, groupby)

from odoo import api, models


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
        results = super().read_group(
            domain,
            fields,
            groupby,
            offset=offset,
            limit=limit,
            orderby=orderby,
            lazy=lazy,
        )
        if _is_current_group_branch(groupby):
            return sorted(results, key=lambda row: _branch_sort_key_from_value(row.get("branch_id")))
        return _sort_groups_by_branch(results, groupby)


def _enable_branch_alpha_on_model(model):
    return bool(model.env.context.get("tradeline_branch_group_alpha"))


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

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
        results = super().read_group(
            domain,
            fields,
            groupby,
            offset=offset,
            limit=limit,
            orderby=orderby,
            lazy=lazy,
        )
        if not _enable_branch_alpha_on_model(self):
            return results

        if _is_current_group_branch(groupby):
            return sorted(results, key=lambda row: _branch_sort_key_from_value(row.get("branch_id")))
        return _sort_groups_by_branch(results, groupby)

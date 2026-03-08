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


def _branch_sort_key(group):
    branch_value = group.get("branch_id")
    if isinstance(branch_value, (list, tuple)) and len(branch_value) >= 2:
        branch_name = branch_value[1] or ""
        return (0, str(branch_name).casefold())
    if isinstance(branch_value, str):
        return (0, branch_value.casefold())
    return (1, "")


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
        if self.env.context.get("tradeline_branch_group_alpha") and _is_current_group_branch(groupby):
            return sorted(results, key=_branch_sort_key)
        return results


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
        if self.env.context.get("tradeline_branch_group_alpha") and _is_current_group_branch(groupby):
            return sorted(results, key=_branch_sort_key)
        return results

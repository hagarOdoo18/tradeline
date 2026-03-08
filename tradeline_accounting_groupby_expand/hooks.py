from odoo import SUPERUSER_ID, api
from odoo.tools.safe_eval import safe_eval


ACTION_XMLIDS = (
    "account.action_account_invoice_report_all",
    "sales_invoice_lines_view.action_account_move_line_sales",
)


def _merge_context(action):
    raw_context = action.context or "{}"
    if isinstance(raw_context, str):
        parsed_context = safe_eval(raw_context, {}) if raw_context.strip() else {}
    elif isinstance(raw_context, dict):
        parsed_context = dict(raw_context)
    else:
        parsed_context = {}

    if not isinstance(parsed_context, dict):
        parsed_context = {}

    changed = False
    if not parsed_context.get("tradeline_groupby_expanded"):
        parsed_context["tradeline_groupby_expanded"] = True
        changed = True
    if not parsed_context.get("tradeline_branch_group_alpha"):
        parsed_context["tradeline_branch_group_alpha"] = True
        changed = True

    if changed:
        action.context = parsed_context


def post_init_hook(env_or_cr, registry=None):
    if registry is None and hasattr(env_or_cr, "cr"):
        env = env_or_cr
    else:
        env = api.Environment(env_or_cr, SUPERUSER_ID, {})

    actions = env["ir.actions.act_window"]
    touched_actions = actions.browse()

    for xmlid in ACTION_XMLIDS:
        action = env.ref(xmlid, raise_if_not_found=False)
        if action and action._name == "ir.actions.act_window":
            touched_actions |= action

    if not touched_actions.filtered(lambda a: a.res_model == "account.invoice.report"):
        touched_actions |= actions.search([("res_model", "=", "account.invoice.report")])

    sales_search_view = env.ref(
        "sales_invoice_lines_view.view_account_move_line_sales_search",
        raise_if_not_found=False,
    )
    if sales_search_view:
        touched_actions |= actions.search([("search_view_id", "=", sales_search_view.id)])

    for action in touched_actions:
        _merge_context(action)

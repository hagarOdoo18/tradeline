from odoo import SUPERUSER_ID, api
from odoo.tools.safe_eval import safe_eval


ACTION_XMLIDS = (
    "account.action_account_invoice_report_all",
    "sales_invoice_lines_view.action_account_move_line_sales",
)


def _context_to_dict(action):
    raw_context = action.context or "{}"
    if isinstance(raw_context, dict):
        return dict(raw_context)
    if not isinstance(raw_context, str):
        return {}
    text = raw_context.strip()
    if not text:
        return {}

    eval_context = {
        "active_id": False,
        "active_ids": [],
        "active_model": False,
        "uid": action.env.uid,
        "user": action.env.user,
        "context": dict(action.env.context),
        "time": __import__("time"),
    }
    try:
        parsed = safe_eval(text, eval_context)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _merge_context(
    action,
    *,
    enable_groupby=False,
    enable_branch_alpha=False,
    enable_time_ranges=False,
):
    parsed_context = _context_to_dict(action)
    if parsed_context is None:
        return

    changed = False
    if enable_groupby and not parsed_context.get("tradeline_groupby_expanded"):
        parsed_context["tradeline_groupby_expanded"] = True
        changed = True
    if enable_branch_alpha and not parsed_context.get("tradeline_branch_group_alpha"):
        parsed_context["tradeline_branch_group_alpha"] = True
        changed = True
    if enable_time_ranges and not parsed_context.get("tradeline_time_ranges_enabled"):
        parsed_context["tradeline_time_ranges_enabled"] = True
        changed = True
    if enable_time_ranges and not parsed_context.get("tradeline_time_engine_enabled"):
        parsed_context["tradeline_time_engine_enabled"] = True
        changed = True
    if enable_time_ranges and not parsed_context.get("tradeline_time_compare"):
        parsed_context["tradeline_time_compare"] = "none"
        changed = True
    if enable_time_ranges and not parsed_context.get("tradeline_time_compare_output"):
        parsed_context["tradeline_time_compare_output"] = "current"
        changed = True

    if enable_time_ranges and not parsed_context.get("tradeline_time_based_on"):
        if action.res_model == "account.invoice.report":
            parsed_context["tradeline_time_based_on"] = "invoice_date"
            changed = True
        elif action.res_model == "account.move.line":
            parsed_context["tradeline_time_based_on"] = "date"
            changed = True

    if changed:
        action.context = parsed_context


def _collect_groupby_actions(env):
    actions = env["ir.actions.act_window"]
    touched_actions = actions.browse()

    for xmlid in ACTION_XMLIDS:
        action = env.ref(xmlid, raise_if_not_found=False)
        if action and action._name == "ir.actions.act_window":
            touched_actions |= action

    touched_actions |= actions.search([("res_model", "=", "account.invoice.report")])

    sales_search_view = env.ref(
        "sales_invoice_lines_view.view_account_move_line_sales_search",
        raise_if_not_found=False,
    )
    if sales_search_view:
        touched_actions |= actions.search([("search_view_id", "=", sales_search_view.id)])

    return touched_actions.exists()


def _collect_time_range_actions(env):
    actions = env["ir.actions.act_window"]
    touched_actions = actions.browse()

    touched_actions |= actions.search([("res_model", "=", "account.invoice.report")])

    sales_search_view = env.ref(
        "sales_invoice_lines_view.view_account_move_line_sales_search",
        raise_if_not_found=False,
    )
    if sales_search_view:
        touched_actions |= actions.search([("search_view_id", "=", sales_search_view.id)])

    report_menus = env["ir.ui.menu"].search([("action", "like", "ir.actions.act_window,%")])
    for menu in report_menus:
        complete_name = (menu.complete_name or "").lower()
        if "report" not in complete_name:
            continue
        try:
            action_id = int((menu.action or "").split(",")[1])
        except Exception:
            continue
        touched_actions |= actions.browse(action_id)

    return touched_actions.exists()


def post_init_hook(env_or_cr, registry=None):
    if registry is None and hasattr(env_or_cr, "cr"):
        env = env_or_cr
    else:
        env = api.Environment(env_or_cr, SUPERUSER_ID, {})

    groupby_actions = _collect_groupby_actions(env)
    for action in groupby_actions:
        _merge_context(action, enable_groupby=True, enable_branch_alpha=True)

    time_range_actions = _collect_time_range_actions(env)
    for action in time_range_actions:
        _merge_context(action, enable_time_ranges=True)

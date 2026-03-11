import logging

from lxml import etree

from odoo import SUPERUSER_ID, api
from odoo.tools.safe_eval import safe_eval


_logger = logging.getLogger(__name__)


GROUPBY_ACTION_XMLIDS = (
    "account.action_account_invoice_report_all",
    "sales_invoice_lines_view.action_account_move_line_sales",
)

DEPRECATED_TIME_FLAGS = (
    "tradeline_time_ranges_enabled",
    "tradeline_time_engine_enabled",
)
LEGACY_TIME_KEYS = (
    "tradeline_time_based_on",
    "tradeline_time_range",
    "tradeline_time_compare",
    "tradeline_time_compare_output",
)

RANGE_SUFFIX_MAP = {
    "last_7_days": "last_7_days",
    "last_30_days": "last_30_days",
    "last_365_days": "last_365_days",
}
FILTER_NAME_BY_MODEL_AND_FIELD = {
    "account.invoice.report": {
        "invoice_date": "tradeline_invoice_date",
        "invoice_date_due": "tradeline_due_date",
    },
    "account.move.line": {
        "date": "tradeline_move_line_date",
        "invoice_date": "tradeline_move_line_invoice_date",
    },
}


def _safe_context_dict(raw_context, env):
    if isinstance(raw_context, dict):
        return dict(raw_context)
    if not isinstance(raw_context, str):
        return {}

    text = (raw_context or "").strip()
    if not text:
        return {}

    eval_context = {
        "active_id": False,
        "active_ids": [],
        "active_model": False,
        "uid": env.uid,
        "user": env.user,
        "company_id": env.company.id,
        "allowed_company_ids": env.companies.ids,
        "context": dict(env.context),
        "time": __import__("time"),
    }
    try:
        parsed = safe_eval(text, eval_context)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_search_arch_date_fields(search_view):
    arch = (search_view.arch_db or "").strip()
    if not arch:
        return []

    try:
        root = etree.fromstring(arch.encode())
    except Exception:
        return []

    date_fields = []
    for node in root.xpath("//filter[@date]"):
        field_name = (node.get("date") or "").strip()
        if field_name and field_name not in date_fields:
            date_fields.append(field_name)
    return date_fields


def _effective_search_view(action):
    """Best-effort resolution of the search view actually used by an action."""
    if action.search_view_id:
        return action.search_view_id

    parsed_context = _safe_context_dict(action.context, action.env) or {}
    search_view_ref = parsed_context.get("search_view_ref")
    if isinstance(search_view_ref, str) and search_view_ref:
        view = action.env.ref(search_view_ref, raise_if_not_found=False)
        if view and view._name == "ir.ui.view" and view.type == "search":
            return view

    if not action.res_model:
        return action.env["ir.ui.view"]

    return action.env["ir.ui.view"].search(
        [
            ("model", "=", action.res_model),
            ("type", "=", "search"),
        ],
        order="priority,id",
        limit=1,
    )


def _collect_reporting_actions(env):
    actions = env["ir.actions.act_window"].browse()
    action_model = env["ir.actions.act_window"]
    menus = env["ir.ui.menu"].search(
        [
            ("action", "like", "ir.actions.act_window,%"),
            "|",
            ("complete_name", "ilike", "report"),
            ("complete_name", "ilike", "reporting"),
        ]
    )
    for menu in menus:
        raw_action = str(menu.action or "").strip()
        if not raw_action.startswith("ir.actions.act_window,"):
            continue
        try:
            action_id = int(raw_action.split(",")[1])
        except Exception:
            continue
        actions |= action_model.browse(action_id)
    return actions.exists()


def _collect_groupby_actions(env):
    actions = env["ir.actions.act_window"].browse()
    for xmlid in GROUPBY_ACTION_XMLIDS:
        action = env.ref(xmlid, raise_if_not_found=False)
        if action and action._name == "ir.actions.act_window":
            actions |= action

    sales_search_view = env.ref(
        "sales_invoice_lines_view.view_account_move_line_sales_search",
        raise_if_not_found=False,
    )
    if sales_search_view:
        actions |= env["ir.actions.act_window"].search([("search_view_id", "=", sales_search_view.id)])
    actions |= env["ir.actions.act_window"].search([("res_model", "=", "account.invoice.report")])
    return actions.exists()


def _merge_action_context(action, enable_groupby=False, enable_branch_alpha=False, enable_native_time=False):
    parsed_context = _safe_context_dict(action.context, action.env)
    if parsed_context is None:
        return False

    changed = False
    if enable_groupby and not parsed_context.get("tradeline_groupby_expanded"):
        parsed_context["tradeline_groupby_expanded"] = True
        changed = True
    if enable_branch_alpha and not parsed_context.get("tradeline_branch_group_alpha"):
        parsed_context["tradeline_branch_group_alpha"] = True
        changed = True
    if enable_native_time and not parsed_context.get("tradeline_time_ranges_native"):
        parsed_context["tradeline_time_ranges_native"] = True
        changed = True

    for key in DEPRECATED_TIME_FLAGS:
        if key in parsed_context:
            parsed_context.pop(key, None)
            changed = True

    if changed:
        action.context = parsed_context
    return changed


def _build_auto_quick_range_arch(date_field, prefix):
    return f"""
<data>
    <xpath expr="//search/filter[@date='{date_field}'][1]" position="inside">
        <filter name="{prefix}_last_7_days" string="Last 7 Days"
                domain="[(\'{date_field}\', \'&gt;=\', (context_today() - relativedelta(days=6)).strftime(\'%Y-%m-%d\')), (\'{date_field}\', \'&lt;=\', context_today().strftime(\'%Y-%m-%d\'))]"/>
        <filter name="{prefix}_last_30_days" string="Last 30 Days"
                domain="[(\'{date_field}\', \'&gt;=\', (context_today() - relativedelta(days=29)).strftime(\'%Y-%m-%d\')), (\'{date_field}\', \'&lt;=\', context_today().strftime(\'%Y-%m-%d\'))]"/>
        <filter name="{prefix}_last_365_days" string="Last 365 Days"
                domain="[(\'{date_field}\', \'&gt;=\', (context_today() - relativedelta(days=364)).strftime(\'%Y-%m-%d\')), (\'{date_field}\', \'&lt;=\', context_today().strftime(\'%Y-%m-%d\'))]"/>
    </xpath>
</data>
""".strip()


def _upsert_auto_quick_range_view(env, search_view, date_field):
    view_name = f"tradeline.reporting.time.ranges.quick.{search_view.id}.{date_field}"
    prefix = f"tradeline_auto_time_{search_view.id}_{date_field}"
    arch_base = _build_auto_quick_range_arch(date_field, prefix)
    view_vals = {
        "name": view_name,
        "type": "search",
        "model": search_view.model,
        "mode": "extension",
        "inherit_id": search_view.id,
        "arch": arch_base,
    }

    view_model = env["ir.ui.view"]
    existing = view_model.search(
        [
            ("name", "=", view_name),
            ("inherit_id", "=", search_view.id),
            ("type", "=", "search"),
        ],
        limit=1,
    )
    if existing:
        existing.write(view_vals)
        return existing
    return view_model.create(view_vals)


def _map_legacy_range_to_native_filter(model_name, based_on, range_key):
    base_name = FILTER_NAME_BY_MODEL_AND_FIELD.get(model_name, {}).get(based_on)
    suffix = RANGE_SUFFIX_MAP.get(range_key)
    if not base_name or not suffix:
        return None
    return f"{base_name}_{suffix}"


def _migrate_favorite_context(record):
    parsed_context = _safe_context_dict(record.context, record.env)
    if parsed_context is None:
        return (False, False)

    has_legacy = any(key in parsed_context for key in LEGACY_TIME_KEYS + DEPRECATED_TIME_FLAGS)
    if not has_legacy:
        return (False, False)

    model_name = record.model_id
    based_on = parsed_context.get("tradeline_time_based_on")
    range_key = parsed_context.get("tradeline_time_range")
    native_filter_name = _map_legacy_range_to_native_filter(model_name, based_on, range_key)

    changed = False
    unmapped = False

    if native_filter_name:
        parsed_context[f"search_default_{native_filter_name}"] = 1
        for key in LEGACY_TIME_KEYS:
            parsed_context.pop(key, None)
        parsed_context.pop("tradeline_time_legacy_mode", None)
        changed = True
    else:
        unmapped = True
        parsed_context["tradeline_time_legacy_mode"] = True
        changed = True

    for key in DEPRECATED_TIME_FLAGS:
        if key in parsed_context:
            parsed_context.pop(key, None)
            changed = True

    if changed:
        record.context = parsed_context
    return (changed, unmapped)


def _migrate_legacy_favorites(env, models_to_scan):
    favorites = env["ir.filters"].search([("model_id", "in", list(models_to_scan))])
    migrated = 0
    unmapped = 0

    for favorite in favorites:
        changed, is_unmapped = _migrate_favorite_context(favorite)
        if changed:
            migrated += 1
        if is_unmapped:
            unmapped += 1

    _logger.info(
        "tradeline_time_ranges: favorites migration complete migrated=%s unmapped=%s scanned=%s",
        migrated,
        unmapped,
        len(favorites),
    )


def _inventory_reporting_actions(actions):
    for action in actions:
        search_view = _effective_search_view(action)
        date_fields = _parse_search_arch_date_fields(search_view) if search_view else []
        _logger.info(
            "tradeline_time_ranges inventory action_id=%s name=%s model=%s action_search_view_id=%s effective_search_view_id=%s date_fields=%s comparison=%s",
            action.id,
            action.name,
            action.res_model,
            action.search_view_id.id if action.search_view_id else False,
            search_view.id if search_view else False,
            date_fields,
            bool(date_fields),
        )


def post_init_hook(env_or_cr, registry=None):
    if registry is None and hasattr(env_or_cr, "cr"):
        env = env_or_cr
    else:
        env = api.Environment(env_or_cr, SUPERUSER_ID, {})

    reporting_actions = _collect_reporting_actions(env)
    groupby_actions = _collect_groupby_actions(env)
    models_to_scan = set()

    _inventory_reporting_actions(reporting_actions)

    for action in reporting_actions:
        if action.res_model:
            models_to_scan.add(action.res_model)
        _merge_action_context(action, enable_native_time=True)

        # For reports outside our dedicated XML patches, inject quick ranges
        # on the first native date filter to make Last 7/30/365 available.
        if action.res_model in {"account.invoice.report", "account.move.line"}:
            continue
        search_view = _effective_search_view(action)
        if not search_view:
            continue
        date_fields = _parse_search_arch_date_fields(search_view)
        if not date_fields:
            continue
        _upsert_auto_quick_range_view(env, search_view, date_fields[0])

    for action in groupby_actions:
        _merge_action_context(action, enable_groupby=True, enable_branch_alpha=True)

    if models_to_scan:
        _migrate_legacy_favorites(env, models_to_scan)

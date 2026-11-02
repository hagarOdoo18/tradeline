"""Explicit administrator-only activation, invoked manually after review/testing."""
import json
from pathlib import Path

from odoo.exceptions import AccessError, ValidationError
from .models.scope import scope_manifest


def activate_business_scope(env, user, expected_database, backup_path):
    if not env.is_superuser() or env.cr.dbname != expected_database:
        raise AccessError("Use an explicit administrator maintenance session and expected database.")
    user.ensure_one()
    if user.id <= 1 or not user.has_group("tradeline_mcp_reporting.group_mcp_reporting_user"):
        raise ValidationError("An already approved named MCP reporting user is required.")
    manifest = scope_manifest(env, user)
    policies = env["ai.copilot.allowed.model"].sudo().search([])
    backup = {"database": env.cr.dbname, "user_id": user.id,
              "models": [{"id": p.id, "model": p.model_name, "mcp_enabled": p.mcp_enabled,
                          "mcp_fields_csv": p.mcp_fields_csv} for p in policies]}
    # Exclusive create: never overwrite an earlier rollback snapshot.
    with Path(backup_path).open("x", encoding="utf-8") as stream:
        json.dump(backup, stream, ensure_ascii=False, indent=2)
    by_name = {p.model_name: p for p in policies}
    for entry in manifest:
        if not entry["reason"]:
            by_name[entry["model"]].write({"mcp_enabled": True,
                "mcp_fields_csv": ",".join(entry["fields"])})
    # Caller owns commit/rollback. No ACL, record rule, business record, or
    # existing ordinary Copilot restriction is modified.
    return {"approved_models": sum(not e["reason"] for e in manifest),
            "excluded_models": [{"model": e["model"], "reason": e["reason"]}
                                for e in manifest if e["reason"]]}

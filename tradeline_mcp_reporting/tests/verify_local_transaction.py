"""Manual integration check via Odoo shell; refuses non-isolated database names.

Creates disposable local fixtures, verifies the real PostgreSQL transaction
boundary (not Odoo's shared TestCursor), and restores all configuration in finally.
Never run against production.
"""
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError


def run(env):
    if not env.cr.dbname.startswith("codex_copilot_qa_"):
        raise RuntimeError("This fixture check is restricted to codex_copilot_qa_* local databases.")
    settings = env["ai.copilot.settings"].sudo().get_singleton()
    old_enabled = settings.enable_mcp_reporting
    policy = env["ai.copilot.allowed.model"].sudo().search([("model_name", "=", "res.partner")], limit=1)
    old_policy = {name: policy[name] for name in ("enabled", "mcp_enabled", "mcp_fields_csv", "allowed_fields_csv")}
    user = visible = hidden = rule = profile = None
    checks = []
    try:
        user = env["res.users"].with_context(no_reset_password=True).create({
            "name": "MCP disposable local integration", "login": "mcp_disposable_local_integration",
            "company_id": env.company.id, "company_ids": [(6, 0, env.company.ids)],
            "groups_id": [(6, 0, env.ref("tradeline_mcp_reporting.group_mcp_reporting_user").ids)]})
        profile = user.partner_id
        visible = env["res.partner"].create({"name": "MCP transaction visible fixture", "company_id": env.company.id})
        hidden = env["res.partner"].create({"name": "MCP transaction hidden fixture", "company_id": env.company.id})
        rule = env["ir.rule"].create({"name": "MCP disposable local rule", "model_id": env.ref("base.model_res_partner").id,
            "domain_force": "[('id','!=',%d)] if user.id == %d else [(1,'=',1)]" % (hidden.id, user.id)})
        settings.enable_mcp_reporting = True
        policy.write({"enabled": True, "mcp_enabled": True, "mcp_fields_csv": "id,name,write_date", "allowed_fields_csv": False})
        env.cr.commit()  # Synthetic LOCAL fixtures must be visible to the fresh read-only connection.
        bridge = env["tradeline.mcp.reporting"].with_user(user)
        info = bridge.execute_tool("connection_info", {})
        assert info["read_only"] and info["company_scope"] == [{"id": user.company_id.id, "name": user.company_id.name}]
        checks.append("named-user identity and main-company scope")
        result = bridge.execute_tool("query", {"spec": {"model": "res.partner", "domain": [["id", "in", [visible.id, hidden.id]]], "fields": ["id", "name"]}})
        assert [row["id"] for row in result["rows"]] == visible.ids
        checks.append("real record rules exclude the hidden record")
        fresh = bridge.execute_tool("data_freshness", {"spec": {"model": "res.partner", "domain": [["id", "=", visible.id]], "date_field": "write_date"}})
        assert fresh["rows"][0]["records"] == 1
        checks.append("date/freshness JSON serialization")
        before = visible.read(["name", "write_date"])[0]

        def unexpected_write(report, tool, arguments):
            report.env.cr.execute("UPDATE res_partner SET name = %s WHERE id = %s", ["MCP write must fail", visible.id])
            return {}

        with patch.object(type(bridge), "_dispatch", unexpected_write):
            try:
                bridge.execute_tool("connection_info", {})
                raise AssertionError("Unexpected write was not blocked")
            except UserError as error:
                assert "read-only" in str(error)
        visible.invalidate_recordset()
        assert visible.read(["name", "write_date"])[0] == before
        checks.append("PostgreSQL rejects unexpected writes; stored values unchanged")
        try:
            bridge.execute_tool("query", {"spec": {"model": "res.partner", "fields": ["phone"]}})
            raise AssertionError("Unapproved field was exposed")
        except AccessError:
            pass
        checks.append("unapproved fields blocked through the real bridge")
        settings.enable_mcp_reporting = False
        env.cr.commit()
        try:
            bridge.execute_tool("connection_info", {})
            raise AssertionError("Off switch was bypassed")
        except AccessError:
            pass
        checks.append("global disable immediately blocks subsequent calls")
    finally:
        env.cr.rollback()
        env.invalidate_all()
        settings.write({"enable_mcp_reporting": old_enabled})
        policy.write(old_policy)
        if rule and rule.exists():
            rule.unlink()
        if user and user.exists():
            user.unlink()
        for record in (visible, hidden, profile):
            if record and record.exists():
                record.unlink()
        env.cr.commit()
    print("MCP_LOCAL_TRANSACTION_CHECKS", len(checks), "PASS", checks)

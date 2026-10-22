"""Build an explicit, reviewable snapshot of current business reporting fields.

No automatic approval at installation, registry refresh, or MCP request time.
"""
import re


EXCLUDED_PREFIXES = (
    "ai.", "ir.", "bus.", "mail.", "base.", "auth", "web.", "web_", "iap.",
    "payment.provider", "payment.token",
    "sign.", "sms.", "snailmail.", "spreadsheet.", "studio.", "discuss.",
    "res.users", "res.config", "res.device", "res.partner.bank", "res.bank",
    "fetchmail.", "oauth.", "calendar.alarm", "digest.", "report.layout",
    "report.paperformat", "tradeline.operations.", "tradeline.executive.report.history",
)
EXCLUDED_SEGMENTS = {"wizard", "configuration", "configurator", "credential", "credentials",
                     "token", "apikey", "connection", "log", "sync", "event", "setting", "settings"}
FIELD_DENY = re.compile(
    r"password|passwd|secret|token|api.?key|oauth|signature|session|private.?key|"
    r"consumer.?key|credential|authorization|endpoint|connection.?string|"
    r"bank.?account|account.?number|iban|bic|swift|passport|national.?id|ssn|"
    r"identification.?id|pin.?code|security.?code", re.I)
FIELD_TYPES = {"boolean", "integer", "float", "monetary", "char", "text", "date", "datetime", "selection", "many2one"}


def excluded_model(service, name):
    if name not in service.env or service._is_model_denied(name):
        return "Technical service or unavailable model"
    model = service.env[name]
    if model._transient or model._abstract:
        return "Wizard or abstract service"
    if name.startswith(EXCLUDED_PREFIXES) or set(name.split(".")) & EXCLUDED_SEGMENTS:
        return "Credentials, integration internals, communication or technical configuration"
    return None


def scope_manifest(env, user, include_fields=True):
    """Metadata only. Field groups/ACLs remain enforced at execution time."""
    service = env["ai.copilot.service"]
    policies = env["ai.copilot.allowed.model"].sudo().search([], order="model_name")
    entries = []
    for policy in policies:
        name = policy.model_name
        reason = excluded_model(service, name)
        if not policy.enabled:
            reason = "Existing Copilot model restriction"
        approved = []
        if not reason:
            model = env[name].with_user(user)
            if not model.has_access("read"):
                reason = "Connected user has no read permission"
            elif include_fields:
                safe = set(service.with_user(user)._safe_fields_for_model(name))
                if policy.allowed_fields_csv:
                    safe &= {v.strip() for v in policy.allowed_fields_csv.split(",")}
                for key in sorted(safe):
                    field = model._fields[key]
                    if (field.type in FIELD_TYPES and (field.store or field.inherited or key == "display_name")
                            and not FIELD_DENY.search(key + " " + str(field.string or ""))
                            and not key.startswith(("message_", "activity_", "website_message_"))):
                        approved.append(key)
                if "id" not in approved:
                    reason = "No readable stable identifier"
        entries.append({"model": name, "label": policy.display_name or name,
                        "modules": sorted(filter(None, (policy.model_id.modules or "").split(","))),
                        "fields": approved if not reason else [], "reason": reason,
                        "mcp_enabled": bool(policy.mcp_enabled)})
    return entries

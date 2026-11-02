"""Fixed-source ORM adapter for the complete repeat-purchase calculation.

Runs only inside the bridge's fresh read-only repeatable-read transaction. Uses
normal ACLs/rules and explicit field policies. No arbitrary model/RPC dispatch.
"""
import json
import time

import psutil
from odoo.exceptions import ValidationError
from odoo.tools.json import json_default
from .repeat_customers import run_report, RepeatReportError


def repeat_report(bridge, spec):
    started = time.monotonic()
    process = psutil.Process()
    initial_memory = process.memory_info().rss
    query = bridge.env["ai.copilot.query"]
    approved, targets = {}, {}

    def budget(unused=None):
        if time.monotonic() - started > 45 or process.memory_info().rss - initial_memory > 160 * 1024 * 1024:
            raise RepeatReportError("The complete repeat-customer scan exceeds the interactive reporting budget. Narrow the years; no partial customer total is available.")

    def call(tool, arguments):
        budget()
        if tool == "connection_info":
            return bridge._dispatch(tool, {})
        if tool in {"describe_model", "data_freshness"}:
            result = getattr(query, tool)(**arguments)
            result = bridge._attach_citation(result, tool, arguments)
        else:
            if tool != "query" or set(arguments) != {"spec"}:
                raise ValidationError("Unsupported repeat report operation.")
            options = arguments["spec"]
            name = options["model"]
            if name not in {"account.move", "pos.order", "legacy.invoice", "res.partner"}:
                raise ValidationError("Unsupported repeat report source.")
            if name not in approved:
                approved[name] = query._readable_fields(name)
                targets[name] = query._model(name)
            domain = options.get("domain", [])
            for field, operator, unused_value in domain:
                if field not in approved[name] or operator not in {"=", "!=", ">", ">=", "<", "<=", "in"}:
                    raise ValidationError("Required source filter is unavailable.")
            model, policy = targets[name]
            if options.get("include_archived"):
                model = model.with_context(active_test=False)
            if options.get("aggregates"):
                if options["aggregates"] != ["__count"] or not policy.allow_groupby:
                    raise ValidationError("Source counting is unavailable.")
                rows, columns, truncated = [{"__count": model.search_count(domain)}], ["__count"], False
            else:
                columns = options["fields"]
                limit = options.get("limit", 100)
                if not set(columns) <= approved[name].keys() or type(limit) is not int or not 1 <= limit <= 500:
                    raise ValidationError("Required repeat report fields are unavailable.")
                order = options.get("order", "id")
                if order not in {"id", "id asc", "id desc"}:
                    raise ValidationError("Unsupported repeat report order.")
                # load=None avoids Many2one sudo display names and their cache
                # amplification. No record names/phones leave this calculation.
                rows = model.search_read(domain, columns, order=order, limit=limit + 1, load=None)
                truncated, rows = len(rows) > limit, rows[:limit]
                for row in rows:
                    for field in columns:
                        if approved[name][field]["type"] == "many2one" and row.get(field):
                            row[field] = [row[field], ""]
            result = {"source": name, "columns": columns, "rows": rows, "truncated": truncated,
                "cells_truncated": False, "provenance": {"model": name, "domain": domain}}
            if options.get("aggregates"):
                result = bridge._attach_citation(result, tool, arguments)
        result = json.loads(json.dumps(result, default=json_default))
        # Read-only cursor: discard clean ORM caches, never flush to database.
        bridge.env.invalidate_all(flush=False)
        budget()
        return result

    result = run_report(call, spec, budget)
    result["read_snapshot"] = "Single PostgreSQL repeatable-read, read-only transaction"
    result["limitations"] = [v for v in result["limitations"] if not v.startswith("Reads span multiple requests")]
    result["provenance"] = {"company": result["company"], "definition": result["definition"], "complete_scan": True}
    return result

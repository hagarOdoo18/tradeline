"""Small, validated read-only tool surface. Never executes model-written code."""
import math
import re
from contextlib import contextmanager

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError


class AICopilotQuery(models.AbstractModel):
    _name = "ai.copilot.query"
    _description = "Copilot Read-only Query Tools"

    OPERATORS = {"=", "!=", ">", ">=", "<", "<=", "in", "not in", "ilike", "not ilike", "like", "=ilike", "=like"}
    AGGREGATES = {"sum", "avg", "min", "max", "count_distinct"}
    GRANULARITIES = {"day", "week", "month", "quarter", "year"}
    HARD_LIMIT = 500
    CELL_LIMIT = 2000

    def _service(self):
        return self.env["ai.copilot.service"]

    def _model(self, name):
        if not isinstance(name, str) or name not in self.env:
            raise ValidationError("Choose an available business model.")
        model, policy = self._service()._check_model_access(name)
        if model._transient:
            raise AccessError("Wizard models are not available.")
        return model, policy

    def _readable_fields(self, name):
        model, policy = self._model(name)
        safe = set(self._service()._safe_fields_for_model(name))
        if policy.allowed_fields_csv:
            safe &= {f.strip() for f in policy.allowed_fields_csv.split(",") if f.strip()}
        def stored_or_delegated(key):
            field = model._fields[key]
            if field.store or key == "display_name":
                return True
            # Delegated _inherits fields live on a parent table. Keep ordinary
            # computed fields excluded; field groups and the model policy apply.
            if not field.inherited or not field.related:
                return False
            path = field.related.split(".") if isinstance(field.related, str) else field.related
            owner = model
            for index, part in enumerate(path):
                if part not in owner._fields:
                    return False
                field = owner._fields[part]
                if index < len(path) - 1:
                    if field.type != "many2one":
                        return False
                    owner = self.env[field.comodel_name]
            return field.store
        return {key: value for key, value in model.fields_get(list(safe)).items()
                if key in safe and stored_or_delegated(key)
                and value.get("type") not in {"one2many", "many2many", "properties", "json"}}

    def _field_path(self, model_name, path):
        if not isinstance(path, str) or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*){0,2}", path):
            raise ValidationError("Invalid field name.")
        parts = path.split(".")
        for index, part in enumerate(parts):
            meta = self._readable_fields(model_name).get(part)
            if not meta:
                raise AccessError("A requested field is unavailable under the data policy.")
            if index < len(parts) - 1:
                if meta["type"] != "many2one":
                    raise ValidationError("Only many-to-one relationships can be traversed.")
                model_name = meta["relation"]
        return meta

    def _domain(self, model_name, domain):
        if not isinstance(domain, list) or len(domain) > 80:
            raise ValidationError("Use a domain list with at most 80 terms.")
        for term in domain:
            if isinstance(term, str) and term in {"&", "|", "!"}:
                continue
            if not isinstance(term, (list, tuple)) or len(term) != 3:
                raise ValidationError("Invalid filter condition.")
            name, operator, value = term
            self._field_path(model_name, name)
            if not isinstance(operator, str) or operator not in self.OPERATORS:
                raise ValidationError("Unsupported filter operator.")
            if operator in {"in", "not in"}:
                if not isinstance(value, list) or len(value) > 500:
                    raise ValidationError("Membership filters require a list of at most 500 values.")
                values = value
            else:
                values = [value]
            if any(not isinstance(v, (str, int, float, bool, type(None))) or
                   (isinstance(v, str) and len(v) > 1000) or
                   (isinstance(v, float) and not math.isfinite(v)) for v in values):
                raise ValidationError("Invalid filter value.")
        # Also validate boolean-domain arity; the ORM validates field value types.
        from odoo.osv import expression
        try:
            expression.normalize_domain(domain)
        except (AssertionError, TypeError, ValueError) as exc:
            raise ValidationError("Invalid boolean filter structure.") from exc
        return domain

    @contextmanager
    def _budget(self):
        # Fixed SQL only controls this transaction's query timeout, never reads data.
        with self.env.cr.savepoint():
            self.env.cr.execute("SELECT current_setting('statement_timeout')")
            previous = self.env.cr.fetchone()[0]
            self.env.cr.execute("SELECT set_config('statement_timeout', %s, true)", ["8000"])
            try:
                yield
            finally:
                # On a failed statement, the savepoint restores the prior setting.
                if self.env.cr._cnx.get_transaction_status() != 3:
                    self.env.cr.execute("SELECT set_config('statement_timeout', %s, true)", [previous])

    @api.model
    def search_models(self, keyword="", offset=0):
        self._service()._assert_internal_user()
        if type(offset) is not int or not 0 <= offset <= 10000:
            raise ValidationError("Use a model search offset between 0 and 10,000.")
        keyword = str(keyword or "").lower()[:100]
        policies = self.env["ai.copilot.allowed.model"].sudo().search([("enabled", "=", True)], order="model_name")
        result = []
        skipped = 0
        for policy in policies:
            if keyword and keyword not in (policy.model_name + " " + (policy.display_name or "") + " " + (policy.model_id.modules or "")).lower():
                continue
            try:
                self._model(policy.model_name)
            except (AccessError, ValidationError):
                continue
            if skipped < offset:
                skipped += 1
                continue
            result.append({"model": policy.model_name, "label": policy.display_name})
            if len(result) == 60:
                break
        return result

    @api.model
    def describe_model(self, model):
        meta = self._readable_fields(model)
        return {"model": model, "fields": {key: {k: v for k, v in val.items()
                if k in {"string", "type", "relation", "selection", "store"}}
                for key, val in meta.items()}}

    @api.model
    def execute_query(self, spec, export=False):
        if not isinstance(spec, dict) or set(spec) - {"model", "domain", "fields", "group_by", "aggregates", "order", "limit", "title", "chart", "include_archived"}:
            raise ValidationError("Unsupported query specification.")
        if "include_archived" in spec and type(spec["include_archived"]) is not bool:
            raise ValidationError("include_archived must be a boolean.")
        if spec.get("include_archived"):
            self = self.with_context(active_test=False)
        name = spec.get("model")
        model, policy = self._model(name)
        if export and not policy.allow_export:
            raise AccessError("Exports are disabled for this model.")
        domain = self._domain(name, spec.get("domain", []))
        meta = self._readable_fields(name)
        groups = spec.get("group_by") or []
        aggregates = spec.get("aggregates") or []
        columns = spec.get("fields") or []
        for items in (groups, aggregates, columns):
            if not isinstance(items, list) or len(items) > 12 or not all(isinstance(v, str) for v in items):
                raise ValidationError("Use at most 12 fields per query section.")
        if groups or aggregates:
            if not policy.allow_groupby:
                raise AccessError("Aggregation is disabled for this model.")
            if len(groups) > 3:
                raise ValidationError("Use at most three grouping dimensions.")
            for group in groups:
                field, _, grain = group.partition(":")
                if field not in meta or not model._fields[field].store:
                    raise AccessError("Grouping field is unavailable.")
                if grain and (grain not in self.GRANULARITIES or meta[field]["type"] not in {"date", "datetime"}):
                    raise ValidationError("Invalid date grouping.")
            for aggregate in aggregates:
                if aggregate == "__count":
                    continue
                field, separator, operation = aggregate.partition(":")
                if field not in meta or not separator or operation not in self.AGGREGATES or not model._fields[field].store:
                    raise AccessError("Aggregation field or operation is unavailable.")
                temporal_extreme = operation in {"min", "max"} and meta[field]["type"] in {"date", "datetime"}
                if operation != "count_distinct" and not temporal_extreme and meta[field]["type"] not in {"integer", "float", "monetary"}:
                    raise ValidationError("This aggregation requires a numeric field.")
            # read_group aliases cannot represent two aggregates of the same field.
            sources = [v.split(":")[0] for v in aggregates if v != "__count"]
            if len(set(sources)) != len(sources):
                raise ValidationError("Use a separate query for each aggregation of the same field.")
        else:
            columns = columns or [f for f in ("name", "display_name", "id") if f in meta][:1]
            if not columns or any(field not in meta for field in columns):
                raise AccessError("A requested output field is unavailable.")

        ceiling = min(self.HARD_LIMIT, max(1, policy.max_rows or 1000))
        if export:
            ceiling = min(50000, max(1, policy.max_rows or 1000))
        requested_limit = spec.get("limit", 100)
        if isinstance(requested_limit, bool) or not isinstance(requested_limit, int) or requested_limit < 1:
            raise ValidationError("Query limit must be a positive integer.")
        limit = min(requested_limit, ceiling)
        order = spec.get("order") or ""
        if not isinstance(order, str) or len(order) > 200:
            raise ValidationError("Invalid sort order.")
        allowed_order = set(meta) if not (groups or aggregates) else {v.split(":")[0] for v in groups + aggregates} | {"__count"}
        for clause in order.split(",") if order else []:
            parts = clause.strip().split()
            if not parts or len(parts) > 2 or parts[0] not in allowed_order or (len(parts) == 2 and parts[1].lower() not in {"asc", "desc"}):
                raise ValidationError("Sort by a readable query field using asc or desc.")
        with self._budget():
            if groups or aggregates:
                raw = model.read_group(domain, [v for v in aggregates if v != "__count"], groups,
                                       limit=limit + 1, orderby=order or False, lazy=False)
                output_columns = groups + aggregates
                if "__count" not in output_columns:
                    output_columns.append("__count")
                rows = []
                for row in raw[:limit]:
                    item = {}
                    for column in output_columns:
                        if column == "__count":
                            item[column] = row.get("__count", 0)
                        elif column in groups:
                            item[column] = row.get(column, row.get(column.split(":")[0], False))
                        else:
                            item[column] = row.get(column.split(":")[0], 0)
                    rows.append(item)
                columns = output_columns
            else:
                raw = model.search_read(domain, columns, limit=limit + 1, order=order or "id")
                rows = [{key: row.get(key) for key in columns} for row in raw[:limit]]

        spec = dict(spec, fields=spec.get("fields") or [], limit=limit)
        labels = {key: ("Records" if key == "__count" else
                  meta[key.split(":")[0]]["string"] + (" (" + key.split(":")[1] + ")" if ":" in key else ""))
                  for key in columns}
        cells_truncated = False
        for row in rows:
            for key, value in row.items():
                if isinstance(value, str) and len(value) > self.CELL_LIMIT:
                    row[key] = value[:self.CELL_LIMIT] + "…"
                    cells_truncated = True
        units = {}
        for key in columns:
            field = model._fields.get(key.split(":")[0])
            if field and field.type == "monetary":
                currency_field = getattr(field, "currency_field", "currency_id")
                if currency_field in model._fields:
                    with self._budget():
                        currencies = model.read_group(domain, [], [currency_field], limit=3, lazy=False)
                    if len(currencies) > 1:
                        if (groups or aggregates) and currency_field not in groups:
                            raise ValidationError("Mixed currencies: include %s in group_by before aggregating monetary values." % currency_field)
                        units[key] = "mixed currencies: group by currency before comparing or totaling"
        filters = []
        for term in domain:
            if isinstance(term, str):
                filters.append({"boolean": {"|": "OR", "&": "AND", "!": "NOT"}[term]})
            else:
                field_meta = self._field_path(name, term[0])
                selection = dict(field_meta.get("selection") or [])
                value = term[2]
                if selection:
                    value = [selection.get(v, v) for v in value] if isinstance(value, list) else selection.get(value, value)
                filters.append({"field": field_meta["string"], "operator": term[1], "value": value})
        provenance = {"model": name, "model_label": str(model._description),
                      "queried_at": fields.Datetime.to_string(fields.Datetime.now()),
                      "companies": [{"id": c.id, "name": c.name} for c in self.env.companies],
                      "domain": domain, "filters": filters, "fields": columns, "group_by": groups, "labels": labels,
                      "aggregates": aggregates or (["__count"] if groups else []),
                      "limit": limit, "returned_rows": len(rows), "truncated": len(raw) > limit,
                      "active_test": bool(self.env.context.get("active_test", True)),
                      "scope": "Saved records visible to your Odoo user; active record rules and branch restrictions apply."}
        provenance["freshness"] = {"mode": "Saved Odoo records", "note": "Query time is not source sync time. Use data_freshness for saved modification / business-date watermarks; upstream completeness is not certified."}
        return {"query": spec, "columns": columns, "labels": labels, "rows": rows,
                "row_count": len(rows), "truncated": len(raw) > limit, "cells_truncated": cells_truncated, "units": units,
                "source": name, "limit": limit, "provenance": provenance}

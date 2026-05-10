from __future__ import annotations

import base64
import csv
import io
import re
import time
from datetime import timedelta
import requests

import xlsxwriter

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.osv import expression


class AICopilotService(models.AbstractModel):
    _name = "ai.copilot.service"
    _description = "AI Copilot Service"

    TECHNICAL_MODEL_PREFIXES = (
        "ir.",
        "bus.",
        "mail.",
        "base.",
        "iap.",
        "web.",
        "digest.",
        "calendar.alarm",
        "auth_",
    )
    TECHNICAL_MODELS = {
        "res.users.apikeys",
        "res.users.log",
        "res.config.settings",
    }

    KEYWORD_MODEL_MAP = {
        "sales": "sale.order",
        "sale": "sale.order",
        "customer": "res.partner",
        "invoice": "account.move",
        "payment": "account.payment",
        "purchase": "purchase.order",
        "supplier": "res.partner",
        "inventory": "stock.quant",
        "stock": "stock.quant",
        "product": "product.product",
    }
    BUSINESS_HINT_KEYWORDS = {
        "sales",
        "sale",
        "sold",
        "sell",
        "units",
        "qty",
        "quantity",
        "customer",
        "invoice",
        "payment",
        "purchase",
        "buy",
        "bought",
        "supplier",
        "inventory",
        "stock",
        "product",
        "top",
        "total",
        "compare",
        "summary",
        "summarize",
        "export",
        "download",
        "csv",
        "excel",
        "xlsx",
        "report",
        "show",
        "list",
        "trend",
        "how many",
        "number of",
        "kpi",
        "orders",
        "bills",
        "receivables",
    }
    SALES_ACTION_KEYWORDS = {
        "sell",
        "sold",
        "sales",
        "revenue",
        "order",
        "orders",
        "gmv",
        "aov",
    }
    PURCHASE_ACTION_KEYWORDS = {
        "buy",
        "bought",
        "purchase",
        "purchases",
        "procurement",
        "vendor",
        "supplier",
    }
    INVENTORY_ACTION_KEYWORDS = {
        "stock",
        "inventory",
        "warehouse",
        "on hand",
        "onhand",
        "availability",
        "available",
        "slow moving",
        "movement",
    }
    ACCOUNTING_ACTION_KEYWORDS = {
        "invoice",
        "invoices",
        "bill",
        "bills",
        "receivable",
        "payable",
        "aging",
        "due",
        "overdue",
    }
    PAYMENT_ACTION_KEYWORDS = {
        "payment",
        "payments",
        "collection",
        "collected",
        "cash",
        "bank",
        "paid",
    }
    MODULE_ROUTES = {
        "sales": {
            "model": "sale.order",
            "keywords": {"sale", "sales", "order", "orders", "quotation", "customer", "revenue"},
        },
        "purchases": {
            "model": "purchase.order",
            "keywords": {"purchase", "purchases", "po", "supplier", "vendor", "procurement", "buy"},
        },
        "inventory": {
            "model": "stock.quant",
            "keywords": {"inventory", "stock", "warehouse", "sku", "onhand", "on-hand", "quantity"},
        },
        "accounting": {
            "model": "account.move",
            "keywords": {"accounting", "invoice", "invoices", "bill", "bills", "receivable", "payable", "aging"},
        },
        "payments": {
            "model": "account.payment",
            "keywords": {"payment", "payments", "collection", "collected", "cash", "bank"},
        },
    }
    PRIORITY_FIELDS_BY_MODEL = {
        "sale.order": [
            "name",
            "date_order",
            "partner_id",
            "user_id",
            "amount_total",
            "state",
            "company_id",
            "currency_id",
        ],
        "purchase.order": [
            "name",
            "date_order",
            "partner_id",
            "user_id",
            "amount_total",
            "state",
            "company_id",
            "currency_id",
        ],
        "stock.quant": [
            "product_id",
            "location_id",
            "quantity",
            "reserved_quantity",
            "available_quantity",
            "company_id",
            "in_date",
        ],
        "account.move": [
            "name",
            "invoice_date",
            "partner_id",
            "move_type",
            "state",
            "amount_total",
            "amount_residual",
            "company_id",
        ],
        "account.payment": [
            "name",
            "date",
            "partner_id",
            "payment_type",
            "state",
            "amount",
            "journal_id",
            "company_id",
        ],
        "res.partner": [
            "name",
            "customer_rank",
            "supplier_rank",
            "email",
            "phone",
            "mobile",
            "company_id",
        ],
    }

    def _assert_internal_user(self):
        if self.env.is_superuser():
            return
        if not self.env.user.has_group("base.group_user"):
            raise AccessError("Only internal Odoo users can access the AI copilot.")

    @api.model
    def get_settings(self):
        self._assert_internal_user()
        return self.env["ai.copilot.settings"].sudo().get_singleton()

    def _get_deny_model_prefixes(self):
        settings = self.get_settings()
        configured = [item.strip() for item in (settings.hard_deny_model_prefixes or "").split(",") if item.strip()]
        return tuple(set(configured + list(self.TECHNICAL_MODEL_PREFIXES)))

    def _get_deny_field_patterns(self):
        settings = self.get_settings()
        configured = [item.strip().lower() for item in (settings.hard_deny_field_patterns or "").split(",") if item.strip()]
        return configured or ["password", "token", "secret", "api_key", "session"]

    def _is_model_denied(self, model_name):
        if model_name in self.TECHNICAL_MODELS:
            return True
        prefixes = self._get_deny_model_prefixes()
        return any(model_name.startswith(prefix) for prefix in prefixes)

    def _is_business_model(self, model_record):
        if model_record.transient:
            return False
        model_name = model_record.model or ""
        if self._is_model_denied(model_name):
            return False
        if model_name.startswith("x_"):
            return True
        # Most business models have at least one of these stable conventions.
        return "." in model_name and not model_name.endswith(".wizard")

    @api.model
    def refresh_allowed_models(self):
        self._assert_internal_user()
        if not self.env.is_superuser() and not self.env.user.has_group("base.group_system"):
            raise AccessError("Only admins can refresh policy models.")

        allowed_model_env = self.env["ai.copilot.allowed.model"].sudo()
        ir_model_env = self.env["ir.model"].sudo()

        existing = {rec.model_name: rec for rec in allowed_model_env.search([])}
        for ir_model in ir_model_env.search([("transient", "=", False)]):
            model_name = ir_model.model
            is_business = self._is_business_model(ir_model)
            if not is_business:
                continue
            values = {
                "display_name": ir_model.name,
                "model_id": ir_model.id,
                "is_business_model": True,
                "enabled": True,
            }
            if model_name in existing:
                existing[model_name].write(values)
            else:
                values["model_name"] = model_name
                allowed_model_env.create(values)
        return True

    def _ensure_allowed_model_entry(self, model_name):
        entry = self.env["ai.copilot.allowed.model"].sudo().search([("model_name", "=", model_name)], limit=1)
        if entry:
            return entry
        ir_model = self.env["ir.model"].sudo().search([("model", "=", model_name), ("transient", "=", False)], limit=1)
        if not ir_model:
            return False
        if not self._is_business_model(ir_model):
            return False
        return self.env["ai.copilot.allowed.model"].sudo().create(
            {
                "model_name": model_name,
                "display_name": ir_model.name,
                "model_id": ir_model.id,
                "enabled": True,
                "is_business_model": True,
            }
        )

    def _resolve_model(self, prompt, context_payload):
        text = (prompt or "").lower()
        for keyword, model_name in self.KEYWORD_MODEL_MAP.items():
            if keyword in text:
                return model_name
        if context_payload and context_payload.get("model"):
            return context_payload["model"]
        return "sale.order"

    def _extract_product_hint(self, prompt):
        text = (prompt or "").strip()
        if not text:
            return False

        quoted = re.search(r"['\"]([^'\"]{2,80})['\"]", text)
        if quoted:
            return quoted.group(1).strip()

        patterns = [
            r"how many\s+(.+?)\s+did\s+we\s+sell",
            r"units?\s+of\s+(.+?)(?:\s+(?:sold|sell|this|last|in)\b|[?.!]|$)",
            r"sales?\s+of\s+(.+?)(?:\s+(?:this|last|in)\b|[?.!]|$)",
            r"for\s+product\s+(.+?)(?:\s+(?:this|last|in)\b|[?.!]|$)",
        ]
        lowered = text.lower()
        for pattern in patterns:
            match = re.search(pattern, lowered)
            if match:
                phrase = match.group(1)
                phrase = re.sub(r"\b(the|a|an|our)\b", "", phrase).strip()
                phrase = re.sub(r"\s+", " ", phrase)
                if len(phrase) >= 2:
                    return phrase
        return False

    def _resolve_date_range(self, prompt):
        text = (prompt or "").lower()
        today = fields.Date.context_today(self)
        if "today" in text:
            return today, today
        if "yesterday" in text:
            yday = today - timedelta(days=1)
            return yday, yday
        if "last month" in text:
            this_start = today.replace(day=1)
            last_end = this_start - timedelta(days=1)
            last_start = last_end.replace(day=1)
            return last_start, last_end
        if "this quarter" in text:
            quarter_month = ((today.month - 1) // 3) * 3 + 1
            start = today.replace(month=quarter_month, day=1)
            return start, today
        if "last quarter" in text:
            quarter_month = ((today.month - 1) // 3) * 3 + 1
            this_q_start = today.replace(month=quarter_month, day=1)
            prev_q_end = this_q_start - timedelta(days=1)
            prev_q_month = ((prev_q_end.month - 1) // 3) * 3 + 1
            prev_q_start = prev_q_end.replace(month=prev_q_month, day=1)
            return prev_q_start, prev_q_end
        if "this year" in text:
            return today.replace(month=1, day=1), today
        if "last year" in text:
            start = today.replace(year=today.year - 1, month=1, day=1)
            end = today.replace(year=today.year - 1, month=12, day=31)
            return start, end
        if "this week" in text:
            start = today - timedelta(days=today.weekday())
            return start, today
        if "last week" in text:
            this_week_start = today - timedelta(days=today.weekday())
            end = this_week_start - timedelta(days=1)
            start = end - timedelta(days=6)
            return start, end
        return today.replace(day=1), today

    def _date_field_for_model(self, model_name, model):
        mapped = {
            "sale.order": "date_order",
            "sale.order.line": "order_id.date_order",
            "purchase.order": "date_order",
            "purchase.order.line": "order_id.date_order",
            "account.move": "invoice_date",
            "account.payment": "date",
            "stock.quant": "in_date",
        }
        if model_name in mapped:
            return mapped[model_name]
        return self._guess_date_field(model)

    def _detect_query_kind(self, prompt):
        text = (prompt or "").lower()
        aggregate_signals = {
            "how many",
            "total",
            "sum",
            "count",
            "top",
            "most",
            "least",
            "average",
            "avg",
            "compare",
        }
        if any(signal in text for signal in aggregate_signals):
            return "aggregate"
        return "list"

    def _route_prompt(self, prompt, context_payload=None):
        text = (prompt or "").strip().lower()
        context_payload = context_payload or {}
        context_model = context_payload.get("model")
        product_hint = self._extract_product_hint(text)
        query_kind = self._detect_query_kind(text)
        explicit_context_ref = any(token in text for token in ["this ", "current ", "selected "])
        if explicit_context_ref and context_model:
            return {
                "intent": "context",
                "module": "context",
                "model_name": context_model,
                "context_model": context_model,
                "query_kind": query_kind,
                "product_hint": product_hint,
            }

        words = set(re.findall(r"[a-z0-9_\\-]+", text))
        scores = {}
        for module_name, cfg in self.MODULE_ROUTES.items():
            score = 0
            for keyword in cfg["keywords"]:
                if keyword in text or keyword in words:
                    score += 1
            if score:
                scores[module_name] = score

        action_score_map = {
            "sales": sum(1 for kw in self.SALES_ACTION_KEYWORDS if kw in text),
            "purchases": sum(1 for kw in self.PURCHASE_ACTION_KEYWORDS if kw in text),
            "inventory": sum(1 for kw in self.INVENTORY_ACTION_KEYWORDS if kw in text),
            "accounting": sum(1 for kw in self.ACCOUNTING_ACTION_KEYWORDS if kw in text),
            "payments": sum(1 for kw in self.PAYMENT_ACTION_KEYWORDS if kw in text),
        }
        for module_name, score in action_score_map.items():
            if score:
                scores[module_name] = scores.get(module_name, 0) + score

        if not scores:
            if product_hint or any(token in text for token in ["sell", "sold", "revenue", "orders", "customers"]):
                scores["sales"] = 1
            elif any(token in text for token in ["vendor", "supplier", "procurement", "buy", "bought"]):
                scores["purchases"] = 1
            elif any(token in text for token in ["invoice", "receivable", "payable", "bill", "aging"]):
                scores["accounting"] = 1
            elif any(token in text for token in ["stock", "inventory", "warehouse", "sku"]):
                scores["inventory"] = 1
            elif context_model:
                return {
                    "intent": "context_default",
                    "module": "context",
                    "model_name": context_model,
                    "context_model": context_model,
                    "query_kind": query_kind,
                    "product_hint": product_hint,
                }
            else:
                scores["sales"] = 1

        top_score = max(scores.values())
        winners = [name for name, score in scores.items() if score == top_score]
        preference_order = ["sales", "purchases", "inventory", "accounting", "payments"]
        module_name = next((module for module in preference_order if module in winners), winners[0])
        model_name = self.MODULE_ROUTES[module_name]["model"]
        if module_name == "accounting" and any(token in text for token in ["payment", "payments", "collection", "collected", "paid"]):
            module_name = "payments"
            model_name = "account.payment"
        if module_name == "sales" and query_kind == "aggregate" and (
            product_hint or any(token in text for token in ["product", "sku", "item", "units", "quantity", "how many"])
        ):
            model_name = "sale.order.line"
        if module_name == "purchases" and query_kind == "aggregate" and any(token in text for token in ["product", "sku", "item", "quantity"]):
            model_name = "purchase.order.line"
        if any(token in text for token in ["customer list", "customers", "partners"]):
            model_name = "res.partner"
        metric_field = False
        group_field = False
        aggregate_op = "sum"
        if model_name == "sale.order.line":
            metric_field = "product_uom_qty"
            if any(token in text for token in ["top", "most", "best"]):
                group_field = "product_id"
        elif model_name == "purchase.order.line":
            metric_field = "product_qty"
            if any(token in text for token in ["top", "most", "best"]):
                group_field = "product_id"
        elif model_name == "sale.order":
            metric_field = "amount_total" if any(token in text for token in ["revenue", "sales", "amount", "value"]) else False
            if any(token in text for token in ["customer", "customers"]):
                group_field = "partner_id"
        elif model_name == "purchase.order":
            metric_field = "amount_total"
            if any(token in text for token in ["supplier", "vendor"]):
                group_field = "partner_id"
        elif model_name == "account.move":
            metric_field = "amount_residual" if any(token in text for token in ["unpaid", "due", "overdue", "receivable"]) else "amount_total"
            if any(token in text for token in ["customer", "customers", "partner"]):
                group_field = "partner_id"
        if any(token in text for token in ["how many orders", "number of orders", "count orders"]):
            aggregate_op = "count"
            metric_field = "id"

        return {
            "intent": "module_query",
            "module": module_name,
            "model_name": model_name,
            "context_model": context_model or False,
            "query_kind": query_kind,
            "metric_field": metric_field,
            "aggregate_op": aggregate_op,
            "group_field": group_field,
            "product_hint": product_hint,
        }

    def _build_router_domain(self, model, route_info, prompt):
        domain = []
        text = (prompt or "").lower()
        model_name = route_info.get("model_name")
        date_field = self._date_field_for_model(model_name, model)
        date_start, date_end = self._resolve_date_range(text)
        if date_field:
            domain.append((date_field, ">=", date_start))
            domain.append((date_field, "<=", date_end))
        if "company_id" in model._fields and self.env.context.get("allowed_company_ids"):
            domain.append(("company_id", "in", self.env.context["allowed_company_ids"]))

        if model_name == "sale.order" and "state" in model._fields:
            domain.append(("state", "in", ["sale", "done"]))
        elif model_name == "sale.order.line":
            domain.append(("order_id.state", "in", ["sale", "done"]))
            if "display_type" in model._fields:
                domain.append(("display_type", "=", False))
        elif model_name == "purchase.order" and "state" in model._fields:
            domain.append(("state", "in", ["purchase", "done"]))
        elif model_name == "purchase.order.line":
            domain.append(("order_id.state", "in", ["purchase", "done"]))
            if "display_type" in model._fields:
                domain.append(("display_type", "=", False))
        elif model_name == "account.move":
            if "state" in model._fields:
                domain.append(("state", "=", "posted"))
            if "move_type" in model._fields:
                if any(token in text for token in ["bill", "bills", "vendor", "supplier"]):
                    domain.append(("move_type", "in", ["in_invoice", "in_refund"]))
                else:
                    domain.append(("move_type", "in", ["out_invoice", "out_receipt", "out_refund"]))
            if "amount_residual" in model._fields and any(token in text for token in ["unpaid", "overdue", "receivable", "due"]):
                domain.append(("amount_residual", ">", 0))
        elif model_name == "account.payment":
            if "state" in model._fields:
                domain.append(("state", "=", "posted"))
        elif model_name == "res.partner":
            if any(token in text for token in ["supplier", "vendor"]) and "supplier_rank" in model._fields:
                domain.append(("supplier_rank", ">", 0))
            elif "customer_rank" in model._fields:
                domain.append(("customer_rank", ">", 0))
        elif model_name == "stock.quant":
            if "quantity" in model._fields and any(token in text for token in ["low stock", "out of stock", "zero stock"]):
                domain.append(("quantity", "<=", 0))
        product_hint = route_info.get("product_hint")
        if product_hint:
            or_filters = []
            if model_name in {"sale.order.line", "purchase.order.line"}:
                or_filters = [
                    [("name", "ilike", product_hint)],
                    [("product_id.display_name", "ilike", product_hint)],
                    [("product_id.default_code", "ilike", product_hint)],
                ]
            elif model_name == "product.product":
                or_filters = [
                    [("name", "ilike", product_hint)],
                    [("default_code", "ilike", product_hint)],
                ]
            elif model_name in {"sale.order", "purchase.order"}:
                or_filters = [[("order_line.name", "ilike", product_hint)]]
            if or_filters:
                domain = expression.AND([domain, expression.OR(or_filters)])
        return domain

    def _prioritized_fields(self, model_name, safe_fields):
        priority = [field for field in self.PRIORITY_FIELDS_BY_MODEL.get(model_name, []) if field in safe_fields]
        rest = [field for field in safe_fields if field not in priority]
        return priority + rest

    def _run_aggregate_query(self, model_name, domain, route_info, limit):
        model = self.env[model_name]
        metric_field = route_info.get("metric_field")
        group_field = route_info.get("group_field")
        aggregate_op = route_info.get("aggregate_op") or "sum"
        if metric_field and metric_field not in model._fields and metric_field != "id":
            metric_field = False
        if group_field and group_field not in model._fields:
            group_field = False

        if aggregate_op == "count":
            field_specs = ["id:count"]
            metric_key = "id_count"
            metric_label = "Count"
        else:
            if not metric_field:
                return False
            field_specs = [f"{metric_field}:sum"]
            metric_key = metric_field
            metric_label = metric_field

        group_by = [group_field] if group_field else []
        grouped_rows = model.read_group(domain, field_specs, group_by, limit=limit, lazy=False)
        rows = []
        for item in grouped_rows:
            entry = {}
            if group_field:
                label_value = item.get(group_field)
                if isinstance(label_value, (list, tuple)):
                    entry[group_field] = label_value[1]
                else:
                    entry[group_field] = label_value or "Undefined"
            value = item.get(metric_key, 0)
            entry[metric_label] = value
            rows.append(entry)

        if not group_field:
            total_value = rows[0][metric_label] if rows else 0
            rows = [{"Metric": metric_label, "Value": total_value}]
            return {"rows": rows, "columns": ["Metric", "Value"], "metric_value": total_value}
        return {
            "rows": rows,
            "columns": [group_field, metric_label],
            "metric_value": sum(float(row.get(metric_label, 0) or 0) for row in rows),
        }

    def _precheck_prompt(self, prompt):
        text = (prompt or "").strip().lower()
        if not text:
            return {
                "intent": "clarification",
                "content": "Please ask a business question, for example: show sales this month.",
            }

        if re.match(r"^(hi|hello|hey|yo|sup|good morning|good afternoon|good evening|thanks|thank you)[\s!.?]*$", text):
            return {
                "intent": "smalltalk",
                "content": (
                    "Hi. I can help with read-only business intelligence in Odoo. "
                    "Try: show sales this month, compare purchases this quarter, or export top customers to Excel."
                ),
            }

        words = re.findall(r"[a-z0-9_]+", text)
        has_numeric_entity = bool(re.search(r"\d", text))
        if len(words) <= 2 and not has_numeric_entity and not any(keyword in text for keyword in self.BUSINESS_HINT_KEYWORDS):
            return {
                "intent": "clarification",
                "content": (
                    "I need a business question to analyze data. "
                    "Example: show top 10 customers by sales this month."
                ),
            }
        return False

    def _parse_top_limit(self, prompt, default_limit, hard_max):
        match = re.search(r"\btop\s+(\d+)\b", prompt or "", flags=re.IGNORECASE)
        if match:
            try:
                value = int(match.group(1))
                return max(1, min(value, hard_max))
            except ValueError:
                return default_limit
        return default_limit

    def _parse_export_intent(self, prompt):
        text = (prompt or "").lower()
        wants_csv = bool(re.search(r"\bcsv\b", text)) or any(
            token in text for token in ["comma separated", "comma-separated"]
        )
        wants_xlsx = any(
            token in text
            for token in ["excel", "xlsx", "spreadsheet", "workbook"]
        )
        wants_generic_export = any(
            token in text
            for token in ["export", "download", "file", "report"]
        )
        if wants_generic_export and not (wants_csv or wants_xlsx):
            wants_xlsx = True
        return wants_csv, wants_xlsx

    def _guess_date_field(self, model):
        for candidate in ("date_order", "invoice_date", "date", "create_date", "scheduled_date", "write_date"):
            if candidate in model._fields:
                return candidate
        return False

    def _default_domain(self, model):
        domain = []
        date_field = self._guess_date_field(model)
        if date_field and model._fields[date_field].type in ("date", "datetime"):
            today = fields.Date.context_today(self)
            start = today.replace(day=1)
            domain.extend([(date_field, ">=", start), (date_field, "<=", today)])
        if "company_id" in model._fields and self.env.context.get("allowed_company_ids"):
            domain.append(("company_id", "in", self.env.context["allowed_company_ids"]))
        return domain

    def _safe_fields_for_model(self, model_name, explicit_fields=None):
        model = self.env[model_name]
        info = model.fields_get()
        deny_patterns = self._get_deny_field_patterns()
        allowed = []
        for field_name, attrs in info.items():
            field_type = attrs.get("type")
            if field_type in ("binary", "html"):
                continue
            lowered = field_name.lower()
            if any(token in lowered for token in deny_patterns):
                continue
            if lowered in {"message_ids", "message_follower_ids", "activity_ids"}:
                continue
            allowed.append(field_name)

        if explicit_fields:
            return [name for name in explicit_fields if name in allowed]
        return allowed

    def _build_table_rows(self, model_name, domain, limit, fields_list):
        model = self.env[model_name]
        if not fields_list:
            raise ValidationError("No safe fields are available for this model.")
        return model.search_read(domain, fields_list, limit=limit)

    def _build_simple_chart(self, rows):
        if not rows:
            return False
        first = rows[0]
        label_field = False
        value_field = False
        for key, value in first.items():
            if isinstance(value, (list, tuple)) and len(value) >= 2:
                label_field = key
                break
            if isinstance(value, str):
                label_field = key
                break
        for key, value in first.items():
            if isinstance(value, (int, float)):
                value_field = key
                break
        if not label_field or not value_field:
            return False
        labels = []
        values = []
        for row in rows[:20]:
            label_val = row.get(label_field)
            if isinstance(label_val, (list, tuple)):
                labels.append(str(label_val[1]))
            else:
                labels.append(str(label_val))
            values.append(float(row.get(value_field) or 0))
        return {
            "type": "chart",
            "chart_type": "bar",
            "title": "%s by %s" % (value_field, label_field),
            "data": {"labels": labels, "datasets": [{"label": value_field, "data": values}]},
        }

    def _format_text_summary(self, model_name, row_count, domain, limit):
        return (
            "I analyzed %s using read-only access with your current permissions. "
            "Returned %s rows (limit %s) and applied filters: %s."
        ) % (model_name, row_count, limit, domain or [])

    def _llm_summary(self, provider, llm_model, prompt, query_meta, rows):
        settings = self.get_settings()
        sample_rows = rows[:10]
        fallback = self._format_text_summary(
            query_meta.get("model_name"),
            query_meta.get("row_count"),
            query_meta.get("domain"),
            query_meta.get("limit"),
        )
        system_prompt = (
            "You are Tradeline AI BI Copilot inside Odoo. "
            "You are strictly read-only. Never claim to create, update, delete, send, confirm, or post anything. "
            "Answer like a sharp business analyst: start with the direct answer in one sentence, then 2-4 short insights. "
            "Mention filters/date limits and whether a row limit was applied. "
            "If the question asked for quantity/count, explicitly state the numeric result. "
            "If data may be incomplete due to permissions or limits, say it clearly."
        )
        user_input = {
            "question": prompt,
            "query_meta": query_meta,
            "sample_rows": sample_rows,
        }

        try:
            timeout = max(5, int(settings.timeout_seconds or 45))
            if provider == "openai" and settings.openai_api_key:
                response = requests.post(
                    "https://api.openai.com/v1/responses",
                    headers={
                        "Authorization": "Bearer %s" % settings.openai_api_key,
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": llm_model or settings.default_openai_model,
                        "temperature": settings.temperature,
                        "max_output_tokens": settings.max_tokens,
                        "input": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": str(user_input)},
                        ],
                    },
                    timeout=timeout,
                )
                response.raise_for_status()
                payload = response.json()
                output_text = payload.get("output_text")
                if output_text:
                    return output_text
            if provider == "claude" and settings.claude_api_key:
                response = requests.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": settings.claude_api_key,
                        "anthropic-version": "2023-06-01",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": llm_model or settings.default_claude_model,
                        "max_tokens": settings.max_tokens,
                        "temperature": settings.temperature,
                        "system": system_prompt,
                        "messages": [{"role": "user", "content": str(user_input)}],
                    },
                    timeout=timeout,
                )
                response.raise_for_status()
                payload = response.json()
                content = payload.get("content") or []
                if content and isinstance(content, list):
                    text_parts = [item.get("text") for item in content if item.get("type") == "text" and item.get("text")]
                    if text_parts:
                        return "\n".join(text_parts)
        except Exception:
            return fallback
        return fallback

    def _create_attachment_file(self, filename, content_bytes, mimetype, conversation, row_count, model_name, file_type, metadata=None):
        attachment = self.env["ir.attachment"].create(
            {
                "name": filename,
                "type": "binary",
                "datas": base64.b64encode(content_bytes),
                "mimetype": mimetype,
                "res_model": "ai.copilot.conversation",
                "res_id": conversation.id if conversation else 0,
            }
        )
        generated = self.env["ai.copilot.generated.file"].create(
            {
                "name": filename,
                "file_type": file_type,
                "attachment_id": attachment.id,
                "user_id": self.env.user.id,
                "conversation_id": conversation.id if conversation else False,
                "source_model": model_name,
                "row_count": row_count,
                "metadata_json": metadata or {},
            }
        )
        return generated

    def _export_csv(self, rows, columns, conversation, model_name):
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column) for column in columns})
        data = output.getvalue().encode("utf-8")
        filename = "ai_copilot_export_%s_%s.csv" % (model_name.replace(".", "_"), fields.Date.context_today(self))
        return self._create_attachment_file(
            filename,
            data,
            "text/csv",
            conversation,
            len(rows),
            model_name,
            "csv",
        )

    def _export_xlsx(self, rows, columns, conversation, model_name, question, provider, llm_model):
        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output, {"in_memory": True})
        fmt_header = workbook.add_format({"bold": True, "bg_color": "#E5E7EB", "border": 1})
        fmt_cell = workbook.add_format({"border": 1})
        summary = workbook.add_worksheet("Summary")
        data_sheet = workbook.add_worksheet("Data")
        meta_sheet = workbook.add_worksheet("Metadata")

        summary.write("A1", "Model")
        summary.write("B1", model_name)
        summary.write("A2", "Rows")
        summary.write("B2", len(rows))

        for col_index, name in enumerate(columns):
            data_sheet.write(0, col_index, name, fmt_header)
            data_sheet.set_column(col_index, col_index, 22)
        for row_index, row_data in enumerate(rows, start=1):
            for col_index, name in enumerate(columns):
                value = row_data.get(name)
                if isinstance(value, (list, tuple)):
                    value = value[1] if len(value) > 1 else value[0]
                data_sheet.write(row_index, col_index, "" if value is None else value, fmt_cell)

        metadata = {
            "Question": question or "",
            "User": self.env.user.name,
            "Date generated": str(fields.Datetime.now()),
            "Odoo model": model_name,
            "Provider": provider or "",
            "Model": llm_model or "",
        }
        meta_row = 0
        for key, value in metadata.items():
            meta_sheet.write(meta_row, 0, key, fmt_header)
            meta_sheet.write(meta_row, 1, value, fmt_cell)
            meta_row += 1

        workbook.close()
        output.seek(0)
        filename = "ai_copilot_export_%s_%s.xlsx" % (model_name.replace(".", "_"), fields.Date.context_today(self))
        return self._create_attachment_file(
            filename,
            output.read(),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            conversation,
            len(rows),
            model_name,
            "xlsx",
            metadata=metadata,
        )

    def _check_model_access(self, model_name):
        if self._is_model_denied(model_name):
            raise AccessError("This model is blocked by policy.")
        entry = self._ensure_allowed_model_entry(model_name)
        if not entry or not entry.enabled:
            raise AccessError("Model is not enabled for copilot usage.")
        model = self.env[model_name]
        model.check_access_rights("read")
        return model, entry

    @api.model
    def run_query(self, prompt, context_payload=None):
        self._assert_internal_user()
        settings = self.get_settings()

        route_info = self._route_prompt(prompt, context_payload=context_payload)
        if route_info.get("needs_clarification"):
            raise ValidationError(route_info["content"])

        model_name = route_info["model_name"]
        model, policy = self._check_model_access(model_name)
        hard_limit = min(policy.max_rows or 1000, settings.max_export_rows_xlsx or 50000)
        default_limit = min(settings.max_preview_rows or 150, hard_limit)
        limit = self._parse_top_limit(prompt, default_limit, hard_limit)

        domain = self._build_router_domain(model, route_info, prompt)
        aggregate_meta = False
        if route_info.get("query_kind") == "aggregate":
            aggregate_meta = self._run_aggregate_query(model_name, domain, route_info, limit)

        if aggregate_meta:
            rows = aggregate_meta["rows"]
            safe_fields = aggregate_meta["columns"]
        else:
            safe_fields = self._safe_fields_for_model(model_name)[:8]
            safe_fields = self._prioritized_fields(model_name, safe_fields)
            if "display_name" not in safe_fields and "name" in model._fields and "name" not in safe_fields:
                safe_fields = ["name"] + safe_fields
            safe_fields = safe_fields[:8]
            rows = self._build_table_rows(model_name, domain, limit, safe_fields)
        return {
            "model_name": model_name,
            "fields": safe_fields,
            "domain": domain,
            "rows": rows,
            "limit": limit,
            "route_intent": route_info.get("intent"),
            "route_module": route_info.get("module"),
            "query_kind": route_info.get("query_kind"),
            "metric_value": aggregate_meta.get("metric_value") if aggregate_meta else False,
            "product_hint": route_info.get("product_hint"),
        }

    @api.model
    def generate_response(self, prompt, conversation=None, context_payload=None, provider=None, llm_model=None):
        self._assert_internal_user()
        start_time = time.time()
        settings = self.get_settings()

        provider = provider or settings.default_provider
        llm_model = llm_model or (
            settings.default_openai_model if provider == "openai" else settings.default_claude_model
        )

        audit_payload = {
            "prompt": prompt,
            "intent": "bi_query",
            "provider": provider,
            "llm_model": llm_model,
            "status": "ok",
        }

        try:
            precheck = self._precheck_prompt(prompt)
            if precheck:
                duration_ms = int((time.time() - start_time) * 1000)
                blocks = [
                    {"type": "text", "content": precheck["content"]},
                ]
                self._log_audit(
                    conversation,
                    {
                        **audit_payload,
                        "intent": precheck["intent"],
                        "row_count": 0,
                        "duration_ms": duration_ms,
                        "file_generated": False,
                    },
                )
                return {
                    "provider": provider,
                    "llm_model": llm_model,
                    "blocks": blocks,
                    "query_meta": {},
                    "file_ids": [],
                }

            query = self.run_query(prompt, context_payload=context_payload)
            model_name = query["model_name"]
            rows = query["rows"]
            columns = query["fields"]
            limit = query["limit"]
            domain = query["domain"]
            route_intent = query.get("route_intent") or "module_query"
            route_module = query.get("route_module") or "unknown"
            query_kind = query.get("query_kind") or "list"
            metric_value = query.get("metric_value")
            product_hint = query.get("product_hint")

            summary_text = self._llm_summary(
                provider,
                llm_model,
                prompt,
                {
                    "model_name": model_name,
                    "row_count": len(rows),
                    "domain": domain,
                    "limit": limit,
                    "query_kind": query_kind,
                    "metric_value": metric_value,
                    "product_hint": product_hint,
                },
                rows,
            )

            text_block = {
                "type": "text",
                "content": summary_text,
            }
            kpi_block = {
                "type": "kpi",
                "items": [
                    {"label": "Module", "value": route_module},
                    {"label": "Model", "value": model_name},
                    {"label": "Rows", "value": len(rows)},
                    {"label": "Limit", "value": limit},
                ],
            }
            if metric_value not in (False, None):
                kpi_block["items"].insert(0, {"label": "Result", "value": metric_value})
            table_block = {"type": "table", "columns": columns, "rows": rows}
            blocks = [text_block, kpi_block, table_block]

            if settings.enable_charts:
                chart_block = self._build_simple_chart(rows)
                if chart_block:
                    blocks.append(chart_block)

            wants_csv, wants_xlsx = self._parse_export_intent(prompt)
            file_entries = []

            if wants_csv and settings.enable_csv:
                csv_file = self._export_csv(rows, columns, conversation, model_name)
                blocks.append(
                    {
                        "type": "download",
                        "label": "Download CSV",
                        "file_id": csv_file.id,
                        "url": csv_file.download_url,
                    }
                )
                file_entries.append(csv_file.id)
            elif wants_csv and not settings.enable_csv:
                blocks.append(
                    {
                        "type": "warning",
                        "content": "CSV export is disabled by administrator settings.",
                    }
                )

            if wants_xlsx and settings.enable_xlsx:
                xlsx_file = self._export_xlsx(rows, columns, conversation, model_name, prompt, provider, llm_model)
                blocks.append(
                    {
                        "type": "download",
                        "label": "Download XLSX",
                        "file_id": xlsx_file.id,
                        "url": xlsx_file.download_url,
                    }
                )
                file_entries.append(xlsx_file.id)
            elif wants_xlsx and not settings.enable_xlsx:
                blocks.append(
                    {
                        "type": "warning",
                        "content": "Excel export is disabled by administrator settings.",
                    }
                )

            duration_ms = int((time.time() - start_time) * 1000)
            audit_payload.update(
                {
                    "intent": route_intent,
                    "model_accessed": model_name,
                    "fields_accessed": ",".join(columns),
                    "domain_json": str(domain),
                    "row_count": len(rows),
                    "duration_ms": duration_ms,
                    "file_generated": bool(file_entries),
                }
            )
            self._log_audit(conversation, audit_payload)

            return {
                "provider": provider,
                "llm_model": llm_model,
                "blocks": blocks,
                "query_meta": {
                    "model_name": model_name,
                    "fields": columns,
                    "domain": domain,
                    "row_count": len(rows),
                    "limit": limit,
                },
                "file_ids": file_entries,
            }
        except ValidationError as exc:
            duration_ms = int((time.time() - start_time) * 1000)
            self._log_audit(
                conversation,
                {
                    **audit_payload,
                    "intent": "clarification",
                    "status": "ok",
                    "row_count": 0,
                    "duration_ms": duration_ms,
                    "file_generated": False,
                },
            )
            return {
                "provider": provider,
                "llm_model": llm_model,
                "blocks": [{"type": "clarification", "content": str(exc)}],
                "query_meta": {},
                "file_ids": [],
            }
        except Exception as exc:
            duration_ms = int((time.time() - start_time) * 1000)
            audit_payload.update(
                {
                    "status": "error",
                    "duration_ms": duration_ms,
                    "error_message": str(exc),
                }
            )
            self._log_audit(conversation, audit_payload)
            raise

    def _log_audit(self, conversation, payload):
        settings = self.get_settings()
        if not settings.enable_audit_logs:
            return
        values = {
            "user_id": self.env.user.id,
            "conversation_id": conversation.id if conversation else False,
            "prompt": payload.get("prompt"),
            "intent": payload.get("intent"),
            "model_accessed": payload.get("model_accessed"),
            "fields_accessed": payload.get("fields_accessed"),
            "domain_json": payload.get("domain_json"),
            "row_count": payload.get("row_count", 0),
            "duration_ms": payload.get("duration_ms", 0),
            "provider": payload.get("provider"),
            "llm_model": payload.get("llm_model"),
            "status": payload.get("status", "ok"),
            "error_message": payload.get("error_message"),
            "file_generated": payload.get("file_generated", False),
        }
        self.env["ai.copilot.audit.log"].sudo().create(values)

    @api.model
    def export_from_query_meta(self, query_meta, file_type, conversation_id=None):
        self._assert_internal_user()
        settings = self.get_settings()
        if file_type not in {"csv", "xlsx"}:
            raise ValidationError("Unsupported export type.")

        model_name = query_meta.get("model_name")
        fields_list = query_meta.get("fields") or []
        domain = query_meta.get("domain") or []
        if not model_name or not fields_list:
            raise ValidationError("Invalid query metadata for export.")

        model, policy = self._check_model_access(model_name)
        limit = settings.max_export_rows_csv if file_type == "csv" else settings.max_export_rows_xlsx
        limit = min(limit or 10000, policy.max_rows or limit)
        rows = model.search_read(domain, fields_list, limit=limit)
        conversation = self.env["ai.copilot.conversation"].browse(conversation_id) if conversation_id else self.env["ai.copilot.conversation"]
        if conversation and conversation_id and conversation.user_id != self.env.user and not self.env.user.has_group("base.group_system"):
            raise AccessError("You can only export your own conversation data.")

        if file_type == "csv":
            generated = self._export_csv(rows, fields_list, conversation, model_name)
        else:
            generated = self._export_xlsx(rows, fields_list, conversation, model_name, "", "", "")
        return {"file_id": generated.id, "url": generated.download_url, "row_count": len(rows)}

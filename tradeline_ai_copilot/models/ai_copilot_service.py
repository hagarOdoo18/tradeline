from __future__ import annotations

import base64
import csv
import io
import json
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
    FOLLOWUP_SIGNALS = {
        "across which",
        "which",
        "breakdown",
        "by color",
        "by branch",
        "by customer",
        "by supplier",
        "by warehouse",
        "what about",
        "and across",
    }
    DATE_QUESTION_SIGNALS = {
        "what day is it",
        "what date is it",
        "today date",
        "today?",
        "date today",
        "day today",
    }
    DEFAULT_SALES_STATES = ["sale", "done"]
    DEFAULT_PURCHASE_STATES = ["purchase", "done"]

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

    def _recent_user_prompts(self, conversation, limit=6):
        if not conversation:
            return []
        messages = self.env["ai.copilot.message"].search(
            [("conversation_id", "=", conversation.id), ("role", "=", "user")],
            order="id desc",
            limit=limit,
        )
        return [item.content or "" for item in messages]

    def _extract_product_hint(self, text):
        prompt = (text or "").strip()
        if not prompt:
            return False
        quoted = re.search(r"['\"]([^'\"]{2,80})['\"]", prompt)
        if quoted:
            return quoted.group(1).strip()
        lowered = prompt.lower()
        patterns = [
            r"how many\s+(.+?)\s+did\s+we\s+sell",
            r"sales?\s+of\s+(.+?)(?:\s+(?:today|yesterday|this|last|in)\b|[?.!]|$)",
            r"units?\s+of\s+(.+?)(?:\s+(?:today|yesterday|this|last|in)\b|[?.!]|$)",
        ]
        for pattern in patterns:
            match = re.search(pattern, lowered)
            if not match:
                continue
            candidate = re.sub(r"\b(the|a|an|our)\b", "", match.group(1)).strip()
            candidate = re.sub(r"\s+", " ", candidate)
            if len(candidate) >= 2:
                return candidate
        return False

    def _resolve_date_range(self, prompt):
        text = (prompt or "").lower()
        today = fields.Date.context_today(self)
        if "today" in text:
            return today, today
        if "yesterday" in text:
            yday = today - timedelta(days=1)
            return yday, yday
        if "last week" in text:
            this_week_start = today - timedelta(days=today.weekday())
            end = this_week_start - timedelta(days=1)
            start = end - timedelta(days=6)
            return start, end
        if "this week" in text:
            start = today - timedelta(days=today.weekday())
            return start, today
        if "last month" in text:
            this_month_start = today.replace(day=1)
            end = this_month_start - timedelta(days=1)
            return end.replace(day=1), end
        if "this quarter" in text:
            start_month = ((today.month - 1) // 3) * 3 + 1
            return today.replace(month=start_month, day=1), today
        if "last quarter" in text:
            current_q_month = ((today.month - 1) // 3) * 3 + 1
            current_q_start = today.replace(month=current_q_month, day=1)
            end = current_q_start - timedelta(days=1)
            prev_q_month = ((end.month - 1) // 3) * 3 + 1
            start = end.replace(month=prev_q_month, day=1)
            return start, end
        if "last year" in text:
            return today.replace(year=today.year - 1, month=1, day=1), today.replace(year=today.year - 1, month=12, day=31)
        if "this year" in text:
            return today.replace(month=1, day=1), today
        return today.replace(day=1), today

    def _contains_followup_signal(self, prompt):
        lowered = (prompt or "").lower()
        return any(signal in lowered for signal in self.FOLLOWUP_SIGNALS)

    def _is_date_question(self, prompt):
        lowered = (prompt or "").lower().strip()
        if any(signal in lowered for signal in self.DATE_QUESTION_SIGNALS):
            return True
        return bool(re.match(r"^what\s+day\s+is\s+it(\s+today)?[?.!]*$", lowered))

    def _extract_dimension(self, prompt):
        text = (prompt or "").lower()
        mapping = {
            "color": "color",
            "colors": "color",
            "branch": "branch",
            "branches": "branch",
            "warehouse": "warehouse",
            "warehouses": "warehouse",
            "customer": "customer",
            "customers": "customer",
            "supplier": "supplier",
            "suppliers": "supplier",
            "product": "product",
            "products": "product",
        }
        for key, dim in mapping.items():
            if key in text:
                return dim
        return False

    def _detect_intent_kind(self, prompt):
        text = (prompt or "").lower()
        if any(token in text for token in ["how many", "count", "number of"]):
            return "count"
        if any(token in text for token in ["total", "sum", "revenue", "amount", "value"]):
            return "sum"
        if any(token in text for token in ["top", "breakdown", "across", "by ", "which"]):
            return "group"
        return "list"

    def _pick_dimension_field(self, model_name, dimension):
        if not dimension:
            return False
        dim_fields = {
            ("sale.order", "branch"): "company_id",
            ("sale.order", "customer"): "partner_id",
            ("sale.order", "product"): "order_line.product_id",
            ("sale.order.line", "product"): "product_id",
            ("sale.order.line", "color"): "product_id",
            ("sale.order.line", "branch"): "company_id",
            ("sale.order.line", "customer"): "order_partner_id",
            ("purchase.order", "branch"): "company_id",
            ("purchase.order", "supplier"): "partner_id",
            ("stock.quant", "warehouse"): "location_id",
            ("stock.quant", "product"): "product_id",
        }
        return dim_fields.get((model_name, dimension))

    def _metric_field_for_model(self, model_name, intent_kind):
        if intent_kind == "count":
            return "id", "count"
        metric_map = {
            "sale.order": "amount_total",
            "sale.order.line": "product_uom_qty",
            "purchase.order": "amount_total",
            "purchase.order.line": "product_qty",
            "account.move": "amount_total",
            "account.payment": "amount",
            "stock.quant": "quantity",
            "res.partner": "id",
            "product.product": "id",
        }
        metric = metric_map.get(model_name) or "id"
        return metric, "sum" if intent_kind in {"sum", "group"} else "count"

    def _conversation_hints(self, prompt, conversation):
        hints = {
            "product_hint": self._extract_product_hint(prompt),
            "module_hint": False,
            "dimension": self._extract_dimension(prompt),
        }
        if hints["product_hint"] and not self._contains_followup_signal(prompt):
            return hints
        for previous in self._recent_user_prompts(conversation):
            if not hints["product_hint"]:
                hints["product_hint"] = self._extract_product_hint(previous)
            lowered = (previous or "").lower()
            if not hints["module_hint"]:
                if any(token in lowered for token in ["sell", "sold", "sales", "order"]):
                    hints["module_hint"] = "sales"
                elif any(token in lowered for token in ["purchase", "supplier", "vendor"]):
                    hints["module_hint"] = "purchase"
                elif any(token in lowered for token in ["invoice", "receivable", "payable"]):
                    hints["module_hint"] = "accounting"
            if hints["product_hint"] and hints["module_hint"]:
                break
        return hints

    def _quick_non_data_response(self, prompt):
        if self._is_date_question(prompt):
            today = fields.Date.context_today(self)
            weekday = today.strftime("%A")
            formatted = today.strftime("%B %d, %Y")
            return {
                "blocks": [
                    {
                        "type": "text",
                        "content": "Today is %s, %s." % (weekday, formatted),
                    }
                ],
                "query_meta": {},
                "file_ids": [],
                "intent": "date_question",
            }
        if re.match(r"^(hi|hello|hey|thanks|thank you)[\s!.?]*$", (prompt or "").lower().strip()):
            return {
                "blocks": [
                    {
                        "type": "text",
                        "content": "Ready. Ask any business question in plain English and I will analyze your Odoo data read-only.",
                    }
                ],
                "query_meta": {},
                "file_ids": [],
                "intent": "smalltalk",
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

    def _build_query_plan(self, prompt, context_payload=None, conversation=None):
        text = (prompt or "").lower()
        context_payload = context_payload or {}
        hints = self._conversation_hints(prompt, conversation)
        model_name = self._resolve_model(prompt, context_payload)
        if hints.get("module_hint") == "sales" and self._contains_followup_signal(prompt):
            model_name = "sale.order.line"
        if hints.get("module_hint") == "purchase" and self._contains_followup_signal(prompt):
            model_name = "purchase.order.line"

        if any(token in text for token in ["sell", "sold", "sales", "units", "quantity", "iphone", "across which"]):
            model_name = "sale.order.line"
        elif any(token in text for token in ["purchase", "bought", "supplier", "vendor"]):
            model_name = "purchase.order"
        elif any(token in text for token in ["invoice", "receivable", "payable", "bill"]):
            model_name = "account.move"
        elif any(token in text for token in ["stock", "inventory", "warehouse"]):
            model_name = "stock.quant"

        intent_kind = self._detect_intent_kind(prompt)
        dimension = hints.get("dimension")
        if not dimension and self._contains_followup_signal(prompt):
            dimension = "product"
        metric_field, metric_op = self._metric_field_for_model(model_name, intent_kind)
        group_field = self._pick_dimension_field(model_name, dimension if intent_kind in {"group", "sum"} else False)
        product_hint = hints.get("product_hint")

        return {
            "model_name": model_name,
            "intent_kind": intent_kind,
            "metric_field": metric_field,
            "metric_op": metric_op,
            "group_field": group_field,
            "dimension": dimension,
            "product_hint": product_hint,
            "date_range": self._resolve_date_range(prompt),
        }

    def _date_field_for_model_name(self, model_name, model):
        mapping = {
            "sale.order": "date_order",
            "sale.order.line": "order_id.date_order",
            "purchase.order": "date_order",
            "purchase.order.line": "order_id.date_order",
            "account.move": "invoice_date",
            "account.payment": "date",
            "stock.quant": "in_date",
        }
        return mapping.get(model_name) or self._guess_date_field(model)

    def _build_planned_domain(self, model_name, model, plan):
        date_start, date_end = plan["date_range"]
        date_field = self._date_field_for_model_name(model_name, model)
        domain = []
        if date_field:
            domain.extend([(date_field, ">=", date_start), (date_field, "<=", date_end)])
        if "company_id" in model._fields and self.env.context.get("allowed_company_ids"):
            domain.append(("company_id", "in", self.env.context["allowed_company_ids"]))
        if model_name == "sale.order" and "state" in model._fields:
            domain.append(("state", "in", self.DEFAULT_SALES_STATES))
        if model_name == "sale.order.line":
            domain.append(("order_id.state", "in", self.DEFAULT_SALES_STATES))
            if "display_type" in model._fields:
                domain.append(("display_type", "=", False))
        if model_name == "purchase.order" and "state" in model._fields:
            domain.append(("state", "in", self.DEFAULT_PURCHASE_STATES))
        if model_name == "purchase.order.line":
            domain.append(("order_id.state", "in", self.DEFAULT_PURCHASE_STATES))
            if "display_type" in model._fields:
                domain.append(("display_type", "=", False))
        if model_name == "account.move":
            if "state" in model._fields:
                domain.append(("state", "=", "posted"))
            if "move_type" in model._fields:
                domain.append(("move_type", "in", ["out_invoice", "out_receipt", "out_refund"]))
        product_hint = plan.get("product_hint")
        if product_hint:
            or_domains = []
            if model_name in {"sale.order.line", "purchase.order.line"}:
                or_domains = [
                    [("name", "ilike", product_hint)],
                    [("product_id.display_name", "ilike", product_hint)],
                    [("product_id.default_code", "ilike", product_hint)],
                ]
            elif model_name in {"sale.order", "purchase.order"}:
                or_domains = [[("order_line.name", "ilike", product_hint)]]
            elif model_name in {"product.product", "product.template"}:
                or_domains = [[("name", "ilike", product_hint)], [("default_code", "ilike", product_hint)]]
            if or_domains:
                domain = expression.AND([domain, expression.OR(or_domains)])
        return domain

    def _aggregate_rows(self, model, plan, domain, limit):
        metric_field = plan.get("metric_field")
        metric_op = plan.get("metric_op")
        group_field = plan.get("group_field")
        label = "Value"

        if metric_op == "count":
            if group_field:
                grouped = model.read_group(domain, ["id:count"], [group_field], limit=limit, lazy=False)
                rows = []
                for item in grouped:
                    group_val = item.get(group_field)
                    label_val = group_val[1] if isinstance(group_val, (list, tuple)) else (group_val or "Undefined")
                    rows.append({group_field: label_val, "Count": int(item.get("id_count") or 0)})
                return rows, [group_field, "Count"], sum(row["Count"] for row in rows)
            count_value = model.search_count(domain)
            return [{"Metric": "Count", "Value": count_value}], ["Metric", "Value"], count_value

        if group_field:
            field_name = metric_field if metric_field in model._fields else "id"
            grouped = model.read_group(domain, [f"{field_name}:sum"], [group_field], limit=limit, lazy=False)
            rows = []
            total = 0.0
            for item in grouped:
                group_val = item.get(group_field)
                label_val = group_val[1] if isinstance(group_val, (list, tuple)) else (group_val or "Undefined")
                metric_value = item.get(field_name)
                if metric_value in (False, None):
                    metric_value = 0
                rows.append({group_field: label_val, "Value": metric_value})
                total += float(metric_value or 0)
            return rows, [group_field, "Value"], total

        fields_list = [metric_field] if metric_field in model._fields else []
        records = model.search_read(domain, fields_list, limit=limit) if fields_list else model.search_read(domain, ["id"], limit=limit)
        total = 0.0
        for row in records:
            value = row.get(metric_field) if fields_list else 1
            if value in (False, None):
                value = 0
            total += float(value or 0)
        label = metric_field or "Value"
        return [{"Metric": label, "Value": total}], ["Metric", "Value"], total

    def _extract_color_from_text(self, text):
        if not text:
            return "Unspecified"
        lowered = str(text).lower()
        colors = [
            "black", "white", "blue", "red", "green", "yellow", "purple", "pink", "gold",
            "silver", "gray", "grey", "orange", "brown", "midnight", "starlight", "natural",
        ]
        for color in colors:
            if color in lowered:
                return color.title()
        return "Unspecified"

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

    def _format_text_summary(self, prompt, query_meta, rows):
        model_name = query_meta.get("model_name") or "data"
        row_count = query_meta.get("row_count", 0)
        date_range = query_meta.get("date_range")
        if date_range:
            start_txt = fields.Date.to_string(date_range[0])
            end_txt = fields.Date.to_string(date_range[1])
            range_txt = "%s to %s" % (start_txt, end_txt)
        else:
            range_txt = "the selected period"
        if row_count == 0:
            return "I checked %s for %s, but there are no matching records with your current filters and access." % (model_name, range_txt)
        if query_meta.get("result_value") not in (False, None):
            return "For %s, the result is %s based on %s matching records in %s." % (
                prompt or "your question",
                query_meta.get("result_value"),
                row_count,
                range_txt,
            )
        return "I analyzed %s and found %s matching records for %s." % (model_name, row_count, range_txt)

    def _llm_summary(self, provider, llm_model, prompt, query_meta, rows):
        settings = self.get_settings()
        sample_rows = rows[:10]
        fallback = self._format_text_summary(prompt, query_meta, rows)
        system_prompt = (
            "You are Tradeline AI BI Copilot inside Odoo. "
            "You are strictly read-only. Never claim to create, update, delete, send, confirm, or post anything. "
            "Return natural business English, not technical logs. "
            "Start with one direct answer sentence, then 2-4 concise insights. "
            "Never mention raw ORM domains, python objects, or internal field names unless asked. "
            "If the question is a quantity/count question, state the numeric result explicitly."
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
                            {"role": "user", "content": json.dumps(user_input, default=str)},
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
                        "messages": [{"role": "user", "content": json.dumps(user_input, default=str)}],
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
    def run_query(self, prompt, context_payload=None, conversation=None):
        self._assert_internal_user()
        settings = self.get_settings()

        plan = self._build_query_plan(prompt, context_payload=context_payload or {}, conversation=conversation)
        model_name = plan["model_name"]
        model, policy = self._check_model_access(model_name)
        hard_limit = min(policy.max_rows or 1000, settings.max_export_rows_xlsx or 50000)
        default_limit = min(settings.max_preview_rows or 150, hard_limit)
        limit = self._parse_top_limit(prompt, default_limit, hard_limit)

        domain = self._build_planned_domain(model_name, model, plan)
        result_value = False
        if plan["intent_kind"] in {"count", "sum", "group"}:
            rows, safe_fields, result_value = self._aggregate_rows(model, plan, domain, limit)
            if plan.get("dimension") == "color" and model_name == "sale.order.line":
                color_map = {}
                for row in rows:
                    label = row.get("product_id") or row.get("Metric") or row.get("Value")
                    color = self._extract_color_from_text(label)
                    color_map[color] = color_map.get(color, 0) + float(row.get("Value") or row.get("Count") or 0)
                color_rows = [{"Color": key, "Value": value} for key, value in sorted(color_map.items(), key=lambda x: x[1], reverse=True)]
                if color_rows:
                    rows = color_rows
                    safe_fields = ["Color", "Value"]
                    result_value = sum(float(item["Value"]) for item in color_rows)
        else:
            safe_fields = self._safe_fields_for_model(model_name)[:8]
            if "display_name" not in safe_fields and "name" in model._fields:
                safe_fields = ["name"] + safe_fields
            safe_fields = safe_fields[:8]
            rows = self._build_table_rows(model_name, domain, limit, safe_fields)
        return {
            "model_name": model_name,
            "fields": safe_fields,
            "domain": domain,
            "rows": rows,
            "limit": limit,
            "plan": plan,
            "result_value": result_value,
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
            quick_response = self._quick_non_data_response(prompt)
            if quick_response:
                duration_ms = int((time.time() - start_time) * 1000)
                audit_payload.update(
                    {
                        "intent": quick_response["intent"],
                        "row_count": 0,
                        "duration_ms": duration_ms,
                        "file_generated": False,
                    }
                )
                self._log_audit(conversation, audit_payload)
                return {
                    "provider": provider,
                    "llm_model": llm_model,
                    "blocks": quick_response["blocks"],
                    "query_meta": quick_response["query_meta"],
                    "file_ids": quick_response["file_ids"],
                }

            query = self.run_query(prompt, context_payload=context_payload, conversation=conversation)
            model_name = query["model_name"]
            rows = query["rows"]
            columns = query["fields"]
            limit = query["limit"]
            domain = query["domain"]
            plan = query.get("plan") or {}
            result_value = query.get("result_value")

            summary_text = self._llm_summary(
                provider,
                llm_model,
                prompt,
                {
                    "model_name": model_name,
                    "row_count": len(rows),
                    "domain": domain,
                    "limit": limit,
                    "result_value": result_value,
                    "date_range": plan.get("date_range"),
                    "intent_kind": plan.get("intent_kind"),
                    "dimension": plan.get("dimension"),
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
                    {"label": "Rows", "value": len(rows)},
                ],
            }
            if result_value not in (False, None):
                kpi_block["items"].insert(0, {"label": "Result", "value": result_value})
            if plan.get("dimension"):
                kpi_block["items"].append({"label": "Breakdown", "value": plan.get("dimension").title()})
            table_block = {"type": "table", "columns": columns, "rows": rows}
            blocks = [text_block, kpi_block]
            if rows:
                blocks.append(table_block)

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
                    "intent": plan.get("intent_kind") or "bi_query",
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

import base64
import hashlib
import json
import logging
import re
from datetime import datetime, time, timedelta

import pytz
from markupsafe import Markup, escape

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools.misc import file_path


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_logger = logging.getLogger(__name__)


class ExecutiveReportSchedule(models.Model):
    _name = "tradeline.executive.report.schedule"
    _description = "Executive Daily and MTD Report Schedule"
    _order = "company_id"

    name = fields.Char(required=True)
    active = fields.Boolean(default=False)
    company_id = fields.Many2one("res.company", required=True, ondelete="cascade", index=True)
    recipient_emails = fields.Char(string="Recipients")
    send_hour = fields.Integer(default=8, help="Local hour, from 0 to 23.")
    timezone = fields.Selection(selection=lambda self: self._tz_selection(), default="Africa/Cairo", required=True)
    top_n = fields.Integer(default=10)
    last_run_at = fields.Datetime(readonly=True)
    last_sent_on = fields.Date(readonly=True)
    next_run_at = fields.Datetime(compute="_compute_next_run_at")
    history_ids = fields.One2many("tradeline.executive.report.history", "schedule_id")

    _sql_constraints = [
        ("company_unique", "unique(company_id)", "Only one executive report schedule is allowed per company."),
    ]

    @api.model
    def _tz_selection(self):
        return [(tz, tz) for tz in pytz.common_timezones]

    @api.constrains("send_hour", "top_n", "recipient_emails", "active")
    def _check_configuration(self):
        for record in self:
            if record.send_hour < 0 or record.send_hour > 23:
                raise ValidationError("Send hour must be between 0 and 23.")
            if record.top_n < 5 or record.top_n > 50:
                raise ValidationError("Top N must be between 5 and 50.")
            invalid = [email for email in record._email_list() if not EMAIL_RE.match(email)]
            if invalid:
                raise ValidationError("Invalid recipient email: %s" % invalid[0])
            if record.active and not record._email_list():
                raise ValidationError("At least one recipient email is required for an active schedule.")

    def _email_list(self):
        self.ensure_one()
        return [item.strip() for item in re.split(r"[,;]", self.recipient_emails or "") if item.strip()]

    def _local_now(self):
        self.ensure_one()
        utc_now = fields.Datetime.now()
        if utc_now.tzinfo is None:
            utc_now = pytz.utc.localize(utc_now)
        return utc_now.astimezone(pytz.timezone(self.timezone or "Africa/Cairo"))

    @api.depends("send_hour", "timezone", "active", "last_sent_on")
    def _compute_next_run_at(self):
        for record in self:
            if not record.active:
                record.next_run_at = False
                continue
            local_now = record._local_now()
            next_date = local_now.date()
            if record.last_sent_on == next_date or local_now.hour >= record.send_hour:
                next_date += timedelta(days=1)
            local_target = pytz.timezone(record.timezone).localize(datetime.combine(next_date, time(record.send_hour)))
            record.next_run_at = local_target.astimezone(pytz.utc).replace(tzinfo=None)

    @api.model
    def _target_companies(self):
        companies = self.env["res.company"].sudo().search([
            "|", ("name", "=ilike", "Tradeline"), ("name", "=ilike", "XPRS"),
        ], order="name")
        return companies or self.env.companies.sudo()

    @api.model
    def _ensure_default_schedules(self):
        companies = self._target_companies()
        existing = self.sudo().with_context(active_test=False)
        recipient = self.env["res.partner"].sudo().search([
            "|", ("name", "ilike", "Mosta"), ("name", "ilike", "Medhat"), ("email", "!=", False),
        ], limit=1)
        if not recipient or not recipient.email:
            recipient = self.env.user.partner_id
        for company in companies:
            if not existing.search_count([("company_id", "=", company.id)]):
                email = recipient.email or ""
                existing.create({
                    "name": "%s Daily + MTD Executive Reports" % company.name,
                    "company_id": company.id,
                    "recipient_emails": email,
                    "active": False,
                    "send_hour": 8,
                    "timezone": "Africa/Cairo",
                    "top_n": 10,
                })

    @api.model
    def _configure_delivery_cron(self):
        """Keep delivery checks frequent without making report generation recurrent."""
        cron = self.env.ref(
            "tradeline_executive_pocket_dashboard.ir_cron_send_executive_daily_reports",
            raise_if_not_found=False,
        )
        if cron:
            cron.sudo().write({
                "interval_number": 5,
                "interval_type": "minutes",
                "nextcall": fields.Datetime.now() + timedelta(minutes=1),
            })
        return True

    @api.model
    def get_dashboard_config(self):
        self.env["tradeline.executive.dashboard.service"]._ensure_exec_admin()
        self._ensure_default_schedules()
        company_ids = self._target_companies().ids
        schedules = self.sudo().with_context(active_test=False).search(
            [("company_id", "in", company_ids)], order="company_id"
        )
        histories = self.env["tradeline.executive.report.history"].sudo().search(
            [("company_id", "in", company_ids)], order="create_date desc", limit=40
        )
        return {
            "schedules": [record._dashboard_values() for record in schedules],
            "history": [record._dashboard_values() for record in histories],
        }

    def _dashboard_values(self):
        self.ensure_one()
        next_run_display = ""
        if self.next_run_at:
            next_utc = self.next_run_at
            if next_utc.tzinfo is None:
                next_utc = pytz.utc.localize(next_utc)
            next_run_display = next_utc.astimezone(pytz.timezone(self.timezone)).strftime("%Y-%m-%d %H:%M %Z")
        return {
            "id": self.id,
            "name": self.name,
            "active": self.active,
            "company_id": self.company_id.id,
            "company_name": self.company_id.name,
            "recipient_emails": self.recipient_emails or "",
            "send_hour": self.send_hour,
            "timezone": self.timezone,
            "top_n": self.top_n,
            "last_run_at": fields.Datetime.to_string(self.last_run_at) if self.last_run_at else "",
            "last_sent_on": fields.Date.to_string(self.last_sent_on) if self.last_sent_on else "",
            "next_run_at": next_run_display,
        }

    @api.model
    def save_dashboard_config(self, schedule_id, values):
        self.env["tradeline.executive.dashboard.service"]._ensure_exec_admin()
        record = self.sudo().browse(int(schedule_id)).exists()
        if not record:
            raise UserError("The report schedule no longer exists.")
        allowed = {"active", "recipient_emails", "send_hour", "timezone", "top_n"}
        clean = {key: value for key, value in (values or {}).items() if key in allowed}
        record.write(clean)
        return record._dashboard_values()

    def action_preview_report(self, report_scope="daily", report_date=None, output_format="html"):
        self.env["tradeline.executive.dashboard.service"]._ensure_exec_admin()
        self.ensure_one()
        if report_scope not in {"daily", "mtd"}:
            raise ValidationError("Report scope must be Daily or MTD.")
        if output_format not in {"html", "pdf"}:
            raise ValidationError("Preview format must be HTML or PDF.")
        target_date = fields.Date.to_date(report_date) if report_date else fields.Date.context_today(self)
        history = self._create_report_history(target_date, delivery_type="preview", report_scope=report_scope)
        if output_format == "html":
            return {
                "type": "ir.actions.act_url",
                "url": "/report/html/tradeline_executive_pocket_dashboard.report_executive_daily_document/%s" % history.id,
                "target": "new",
            }
        attachment = history._render_and_attach_pdf()
        return {
            "type": "ir.actions.act_url",
            "url": "/web/content/%s?download=1" % attachment.id,
            "target": "self",
        }

    def action_preview_pdf(self, report_date=None):
        """Backward-compatible Daily PDF preview for older cached assets."""
        return self.action_preview_report("daily", report_date, "pdf")

    def _create_report_history(self, report_date, delivery_type="scheduled", report_scope="daily"):
        self.ensure_one()
        period_start = report_date if report_scope == "daily" else report_date.replace(day=1)
        filters = {
            "company_ids": [self.company_id.id],
            "branch_ids": [],
            "salesperson_ids": [],
            "start_date": fields.Date.to_string(period_start),
            "end_date": fields.Date.to_string(report_date),
            "report_date": fields.Date.to_string(report_date),
            "product_category": "all",
            "inventory_category": "all",
        }
        # Company-level executive PDFs must not inherit the scheduler user's
        # personal branch assignments; the dashboard itself keeps that behavior.
        service = self.env["tradeline.executive.dashboard.service"].with_context(
            exec_report_all_branches=True
        )
        bundle = service.get_dashboard_bundle(
            filters, "overview", None, 10,
        )
        report_scope_data = service._resolve_filter_scope(filters)
        margin_status = service._real_margin_availability(report_scope_data)
        top_sections = bundle.setdefault("top_sections", {})
        branch_rows = service._all_sales_by_branch(report_scope_data, margin_status)
        if service._mostafa_margin_enabled(report_scope_data, margin_status):
            service._apply_mostafa_margin(
                branch_rows, report_scope_data, "branch", margin_status
            )
        top_sections["sales_by_branch"] = branch_rows
        top_sections["sales_by_branch_pages"] = [
            branch_rows[index:index + 18]
            for index in range(0, len(branch_rows), 18)
        ] or [[]]
        top_sections["inventory_by_family"] = service._top_inventory_by_family(
            report_scope_data, 10
        )
        # Both report variants use a complete month-to-date calendar trend.
        trend_filters = dict(filters)
        trend_filters["start_date"] = fields.Date.to_string(report_date.replace(day=1))
        trend_scope = service._resolve_filter_scope(trend_filters)
        bundle.setdefault("top_sections", {})["sales_over_month"] = service._sales_over_month(trend_scope)
        return self.env["tradeline.executive.report.history"].sudo().create({
            "schedule_id": self.id,
            "company_id": self.company_id.id,
            "recipient_emails": self.recipient_emails or "",
            "delivery_type": delivery_type,
            "report_scope": report_scope,
            "report_date": report_date,
            "period_start": period_start,
            "period_end": report_date,
            "state": "preview" if delivery_type == "preview" else "pending",
            "payload_json": json.dumps(bundle, ensure_ascii=False, default=str),
        })

    def _send_for_date(self, report_date):
        self.ensure_one()
        daily_history = self._create_report_history(report_date, report_scope="daily")
        mtd_history = self._create_report_history(report_date, report_scope="mtd")
        histories = daily_history | mtd_history
        try:
            with self.env.cr.savepoint():
                attachments = histories.mapped(lambda history: history._render_and_attach_pdf())
                mail = self.env["mail.mail"].sudo().create({
                    "subject": "%s - Daily + MTD Executive Reports - %s" % (self.company_id.name, report_date),
                    "email_to": ",".join(self._email_list()),
                    "body_html": (
                        "<p>Please find attached the <strong>%s Daily Executive Brief</strong> and "
                        "<strong>MTD Executive Report</strong> for %s.</p>"
                        "<p>Both reports include all branches, untaxed sales, margin, customers, "
                        "payment journals, inventory by category, product family and product "
                        "variant, plus source definitions.</p>"
                    ) % (self.company_id.name, report_date),
                    "attachment_ids": [(4, attachment.id) for attachment in attachments],
                    "auto_delete": False,
                })
                mail.send(raise_exception=True)
                histories.write({"state": "sent", "sent_at": fields.Datetime.now(), "mail_id": mail.id})
                self.write({"last_run_at": fields.Datetime.now(), "last_sent_on": report_date})
        except Exception as exc:
            if histories:
                histories.write({"state": "failed", "error_message": str(exc), "sent_at": fields.Datetime.now()})
            self.write({"last_run_at": fields.Datetime.now()})
            _logger.exception("Executive report delivery failed for %s", self.company_id.name)
        return histories

    @api.model
    def _cron_send_due_reports(self):
        self._ensure_default_schedules()
        target_company_ids = self._target_companies().ids
        for schedule in self.sudo().search([
            ("active", "=", True), ("company_id", "in", target_company_ids),
        ]):
            local_now = schedule._local_now()
            report_date = local_now.date() - timedelta(days=1)
            if local_now.hour < schedule.send_hour or schedule.last_sent_on == report_date:
                continue

            # A schedule enabled after today's target time starts tomorrow; it
            # must not immediately backfill an older report before midnight.
            schedule_write = fields.Datetime.to_datetime(schedule.write_date)
            if schedule_write:
                if schedule_write.tzinfo is None:
                    schedule_write = pytz.utc.localize(schedule_write)
                local_write = schedule_write.astimezone(pytz.timezone(schedule.timezone))
                local_target = pytz.timezone(schedule.timezone).localize(
                    datetime.combine(local_now.date(), time(schedule.send_hour))
                )
                if local_write > local_target:
                    continue
            try:
                with self.env.cr.savepoint():
                    schedule._send_for_date(report_date)
            except Exception:
                # Isolate a pre-history data failure so remaining companies run.
                _logger.exception("Executive report delivery failed for %s", schedule.company_id.name)
                continue


class ExecutiveReportHistory(models.Model):
    _name = "tradeline.executive.report.history"
    _description = "Executive Report Delivery History"
    _order = "create_date desc"

    schedule_id = fields.Many2one("tradeline.executive.report.schedule", ondelete="set null", index=True)
    company_id = fields.Many2one("res.company", required=True, index=True)
    recipient_emails = fields.Char()
    delivery_type = fields.Selection([("scheduled", "Scheduled"), ("preview", "Preview")], required=True)
    report_scope = fields.Selection(
        [("daily", "Daily"), ("mtd", "Month to Date")], required=True, default="mtd", index=True,
    )
    report_date = fields.Date(required=True, index=True)
    period_start = fields.Date(required=True)
    period_end = fields.Date(required=True)
    state = fields.Selection([
        ("pending", "Pending"), ("preview", "Preview"), ("sent", "Sent"), ("failed", "Failed"),
    ], required=True, default="pending", index=True)
    sent_at = fields.Datetime()
    error_message = fields.Text()
    payload_json = fields.Text(required=True)
    attachment_id = fields.Many2one("ir.attachment", ondelete="set null")
    mail_id = fields.Many2one("mail.mail", ondelete="set null")

    def get_report_payload(self):
        self.ensure_one()
        return json.loads(self.payload_json or "{}")

    def _render_and_attach_pdf(self):
        self.ensure_one()
        if self.attachment_id:
            return self.attachment_id
        action = self.env.ref("tradeline_executive_pocket_dashboard.action_report_executive_daily_pdf")
        pdf_content, _ = action._render_qweb_pdf(action.report_name, res_ids=self.ids)
        filename = "%s_%s_Executive_Report_%s.pdf" % (
            re.sub(r"[^A-Za-z0-9_-]+", "_", self.company_id.name or "Company"),
            self.report_scope.upper(),
            self.report_date,
        )
        attachment = self.env["ir.attachment"].sudo().create({
            "name": filename,
            "type": "binary",
            "datas": base64.b64encode(pdf_content),
            "mimetype": "application/pdf",
            "res_model": self._name,
            "res_id": self.id,
        })
        self.attachment_id = attachment
        return attachment

    def _dashboard_values(self):
        self.ensure_one()
        return {
            "id": self.id,
            "company_name": self.company_id.name,
            "report_date": fields.Date.to_string(self.report_date),
            "delivery_type": self.delivery_type,
            "report_scope": self.report_scope,
            "report_scope_label": dict(self._fields["report_scope"].selection).get(self.report_scope),
            "state": self.state,
            "recipient_emails": self.recipient_emails or "",
            "sent_at": fields.Datetime.to_string(self.sent_at) if self.sent_at else "",
            "error_message": self.error_message or "",
            "download_url": "/web/content/%s?download=1" % self.attachment_id.id if self.attachment_id else "",
        }

    def report_currency(self, value):
        return "EGP {:,.0f}".format(float(value or 0))

    def report_logo_data_uri(self):
        self.ensure_one()
        filename = "xprs_logo.png" if "xprs" in (self.company_id.name or "").lower() else "tradeline_mark.png"
        try:
            path = file_path(
                "tradeline_executive_pocket_dashboard/static/description/%s" % filename
            )
            with open(path, "rb") as logo_file:
                encoded = base64.b64encode(logo_file.read()).decode("ascii")
        except (OSError, ValueError):
            _logger.exception("Executive report logo could not be loaded: %s", filename)
            return ""
        return "data:image/png;base64,%s" % encoded

    def report_dimension_label(self, value):
        """Keep Unicode labels out of wkhtmltopdf's broken HTML charset path."""
        text = self.env["tradeline.executive.dashboard.service"]._repair_mojibake(
            str(value or "Unassigned")
        )
        if text.isascii():
            return escape(text)

        # QWeb resolves HTML entities before wkhtmltopdf receives the document.
        # CSS escapes remain ASCII until Qt's renderer constructs the glyphs.
        css_text = "".join("\\%x " % ord(character) for character in text)
        css_class = "o_pdf_label_%s" % hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
        return Markup(
            '<span class="%s" dir="auto"></span>'
            '<style>.%s:before { content: "%s"; }</style>'
        ) % (css_class, css_class, css_text)

    def report_compact(self, value, prefix=""):
        number = float(value or 0)
        absolute = abs(number)
        sign = "-" if number < 0 else ""
        if absolute >= 1_000_000_000:
            text = "{:.1f}B".format(absolute / 1_000_000_000)
        elif absolute >= 1_000_000:
            text = "{:.1f}M".format(absolute / 1_000_000)
        elif absolute >= 1_000:
            text = "{:.1f}K".format(absolute / 1_000)
        else:
            text = "{:,.0f}".format(absolute)
        return "%s%s%s" % (prefix, sign, text)

    def report_number(self, value):
        return "{:,.0f}".format(float(value or 0))

    def report_percent(self, value):
        return "{:.1f}%".format(float(value or 0))

    def report_bar_width(self, value, rows, key):
        maximum = max([abs(float(row.get(key) or 0)) for row in (rows or [])] or [1.0])
        width = (abs(float(value or 0)) / maximum) * 100 if maximum else 0
        return "%.1f%%" % max(2.0, min(width, 100.0))

    def report_period_label(self):
        self.ensure_one()
        if self.report_scope == "daily":
            return self.report_date.strftime("%d %B %Y")
        return "%s - %s" % (self.period_start.strftime("%d %B"), self.period_end.strftime("%d %B %Y"))

    def report_title(self):
        self.ensure_one()
        return "Daily Executive Brief" if self.report_scope == "daily" else "MTD Executive Report"

    def report_card_value(self, card):
        self.ensure_one()
        if card.get("unit") == "EGP":
            return self.report_compact(card.get("value"), "EGP ")
        if card.get("unit") == "%":
            return self.report_percent(card.get("value"))
        return self.report_compact(card.get("value"))

    def report_trend_chart(self, rows):
        self.ensure_one()
        source_rows = {
            str(row.get("date")): row for row in (rows or []) if row.get("date")
        }
        start_date = self.report_date.replace(day=1)
        calendar_rows = []
        current_date = start_date
        while current_date <= self.report_date:
            date_key = fields.Date.to_string(current_date)
            source = source_rows.get(date_key, {})
            calendar_rows.append({
                "date": date_key,
                "net_revenue": float(source.get("net_revenue") or 0),
            })
            current_date += timedelta(days=1)
        if not calendar_rows:
            return {"line": "", "area": "", "points": [], "x_ticks": [], "y_ticks": []}

        left, right, top, bottom = 58.0, 510.0, 10.0, 121.0
        values = [row["net_revenue"] for row in calendar_rows]
        low, high = min(values), max(values)
        axis_low, axis_high = min(0.0, low), max(0.0, high)
        if axis_high == axis_low:
            axis_high = axis_low + max(abs(axis_low), 1.0)
        spread = axis_high - axis_low
        points = []
        for index, (row, value) in enumerate(zip(calendar_rows, values)):
            x = left if len(calendar_rows) == 1 else left + index * ((right - left) / (len(calendar_rows) - 1))
            y = top + (1.0 - ((value - axis_low) / spread)) * (bottom - top)
            points.append({"x": round(x, 2), "y": round(y, 2), "date": row.get("date"), "value": value})
        line = " ".join(("M" if index == 0 else "L") + " %.2f %.2f" % (point["x"], point["y"]) for index, point in enumerate(points))
        zero_y = top + (1.0 - ((0.0 - axis_low) / spread)) * (bottom - top)
        area = "%s L %.2f %.2f L %.2f %.2f Z" % (
            line, points[-1]["x"], zero_y, points[0]["x"], zero_y,
        )

        y_ticks = []
        for index in range(5):
            value = axis_low + (spread * index / 4.0)
            y = top + (1.0 - ((value - axis_low) / spread)) * (bottom - top)
            y_ticks.append({"y": round(y, 2), "label": self.report_compact(value, "EGP ")})

        tick_count = min(6, len(points))
        tick_indexes = sorted({
            round(index * (len(points) - 1) / max(tick_count - 1, 1))
            for index in range(tick_count)
        })
        x_ticks = []
        for position, index in enumerate(tick_indexes):
            point = points[index]
            label = datetime.strptime(point["date"], "%Y-%m-%d").strftime("%d %b")
            anchor = "start" if position == 0 else ("end" if position == len(tick_indexes) - 1 else "middle")
            x_ticks.append({"x": point["x"], "label": label, "anchor": anchor})
        return {
            "line": line,
            "area": area,
            "points": points,
            "x_ticks": x_ticks,
            "y_ticks": y_ticks,
            "axis_left": left,
            "axis_right": right,
            "axis_top": top,
            "axis_bottom": bottom,
            "zero_y": round(zero_y, 2),
            "high": high,
            "low": low,
            "total": sum(values),
        }

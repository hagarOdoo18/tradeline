import base64
import json
import logging
import re
from datetime import datetime, time, timedelta

import pytz

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_logger = logging.getLogger(__name__)


class ExecutiveReportSchedule(models.Model):
    _name = "tradeline.executive.report.schedule"
    _description = "Executive Daily Report Schedule"
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
    def _ensure_default_schedules(self):
        companies = self.env["res.company"].sudo().search([])
        recipient = self.env["res.partner"].sudo().search([
            "|", ("name", "ilike", "Mosta"), ("name", "ilike", "Medhat"), ("email", "!=", False),
        ], limit=1)
        if not recipient or not recipient.email:
            recipient = self.env.user.partner_id
        for company in companies:
            if not self.sudo().search_count([("company_id", "=", company.id)]):
                email = recipient.email or ""
                self.sudo().create({
                    "name": "%s Daily Executive Report" % company.name,
                    "company_id": company.id,
                    "recipient_emails": email,
                    "active": bool(email),
                    "send_hour": 8,
                    "timezone": "Africa/Cairo",
                    "top_n": 10,
                })

    @api.model
    def get_dashboard_config(self):
        self.env["tradeline.executive.dashboard.service"]._ensure_exec_admin()
        self._ensure_default_schedules()
        schedules = self.sudo().search([], order="company_id")
        histories = self.env["tradeline.executive.report.history"].sudo().search([], order="create_date desc", limit=30)
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

    def action_preview_pdf(self, report_date=None):
        self.env["tradeline.executive.dashboard.service"]._ensure_exec_admin()
        self.ensure_one()
        target_date = fields.Date.to_date(report_date) if report_date else fields.Date.context_today(self)
        history = self._create_report_history(target_date, delivery_type="preview")
        attachment = history._render_and_attach_pdf()
        return {
            "type": "ir.actions.act_url",
            "url": "/web/content/%s?download=1" % attachment.id,
            "target": "self",
        }

    def _create_report_history(self, report_date, delivery_type="scheduled"):
        self.ensure_one()
        period_start = report_date.replace(day=1)
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
        bundle = self.env["tradeline.executive.dashboard.service"].get_dashboard_bundle(
            filters, "overview", None, self.top_n,
        )
        return self.env["tradeline.executive.report.history"].sudo().create({
            "schedule_id": self.id,
            "company_id": self.company_id.id,
            "recipient_emails": self.recipient_emails or "",
            "delivery_type": delivery_type,
            "report_date": report_date,
            "period_start": period_start,
            "period_end": report_date,
            "state": "preview" if delivery_type == "preview" else "pending",
            "payload_json": json.dumps(bundle, ensure_ascii=False, default=str),
        })

    def _send_for_date(self, report_date):
        self.ensure_one()
        history = self._create_report_history(report_date)
        try:
            with self.env.cr.savepoint():
                attachment = history._render_and_attach_pdf()
                mail = self.env["mail.mail"].sudo().create({
                    "subject": "%s - Daily Executive Report - %s" % (self.company_id.name, report_date),
                    "email_to": ",".join(self._email_list()),
                    "body_html": (
                        "<p>Please find attached the <strong>%s</strong> executive report for %s.</p>"
                        "<p>The report covers month-to-date performance through the report day and includes "
                        "sales, margin, customers, journals, inventory, FX, and source definitions.</p>"
                    ) % (self.company_id.name, report_date),
                    "attachment_ids": [(4, attachment.id)],
                    "auto_delete": False,
                })
                mail.send(raise_exception=True)
                history.write({"state": "sent", "sent_at": fields.Datetime.now(), "mail_id": mail.id})
                self.write({"last_run_at": fields.Datetime.now(), "last_sent_on": report_date})
        except Exception as exc:
            history.write({"state": "failed", "error_message": str(exc), "sent_at": fields.Datetime.now()})
            self.write({"last_run_at": fields.Datetime.now()})
            _logger.exception("Executive report delivery failed for %s", self.company_id.name)
        return history

    @api.model
    def _cron_send_due_reports(self):
        self._ensure_default_schedules()
        for schedule in self.sudo().search([("active", "=", True)]):
            local_now = schedule._local_now()
            if local_now.hour < schedule.send_hour or schedule.last_sent_on == local_now.date():
                continue
            try:
                with self.env.cr.savepoint():
                    schedule._send_for_date(local_now.date() - timedelta(days=1))
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
        filename = "%s_Executive_Report_%s.pdf" % (
            re.sub(r"[^A-Za-z0-9_-]+", "_", self.company_id.name or "Company"),
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
            "state": self.state,
            "recipient_emails": self.recipient_emails or "",
            "sent_at": fields.Datetime.to_string(self.sent_at) if self.sent_at else "",
            "error_message": self.error_message or "",
            "download_url": "/web/content/%s?download=1" % self.attachment_id.id if self.attachment_id else "",
        }

    def report_currency(self, value):
        return "EGP {:,.0f}".format(float(value or 0))

    def report_number(self, value):
        return "{:,.0f}".format(float(value or 0))

    def report_percent(self, value):
        return "{:.1f}%".format(float(value or 0))

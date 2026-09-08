# -*- coding: utf-8 -*-
"""
Pinned Report Engine (curated reports)
======================================
Hand-tuned, audited reports over the whole system -- invoices, payments,
sales, purchases, inventory, POS and HR.  They are used in two ways:

  * as AI tools -- the assistant calls ``run_report(name, ...)`` when a
    curated report answers the question better than a generic query;
  * as the no-AI fallback -- ``get_context_for_message()`` keyword-routes
    the question to the closest report when no AI provider is configured.

Anything not covered here is handled by the generic engine
``ai.data.query.engine``, which can reach any allowed model.
"""
import logging
import re
from datetime import date, timedelta

from odoo import models, api

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Intent keywords (Arabic + English)
# ---------------------------------------------------------------------------
_KW_SUMMARY   = ['summary', 'total', 'overview', 'ملخص', 'إجمالي', 'مجموع']
_KW_OVERDUE   = ['overdue', 'unpaid', 'open', 'outstanding', 'متأخر', 'غير مدفوع', 'مفتوح']
_KW_BRANCH    = ['branch', 'journal', 'فرع', 'يومية', 'طريقة دفع', 'payment method']
_KW_DATE      = ['date', 'range', 'from', 'to', 'period', 'month', 'year',
                 'تاريخ', 'فترة', 'شهر', 'سنة', 'من', 'إلى']
_KW_CUSTOMER  = ['customer', 'client', 'partner', 'عميل', 'زبون']
_KW_PAID      = ['paid', 'collected', 'received', 'مدفوع', 'مستوفى', 'محصّل']
_KW_INV_TYPE  = ['type', 'invoice type', 'credit', 'refund', 'by type',
                 'نوع', 'نوع الفاتورة', 'إشعار دائن', 'إشعار', 'تحليل بالنوع',
                 'analysis', 'تحليل']
_KW_TOP_CUST  = ['top customer', 'best customer', 'most sale', 'highest sale',
                 'top client', 'customer ranking', 'افضل عميل', 'أفضل عميل',
                 'أعلى مبيعات', 'اعلى مبيعات', 'أكثر عميل', 'ترتيب العملاء',
                 'top', 'ranking', 'most', 'best', 'ترتيب', 'أعلى', 'افضل']

# --- non-invoice domains ---------------------------------------------------
_KW_SALES_ORD = ['sales order', 'sale order', 'quotation', 'quotations', 'so ',
                 'أمر بيع', 'أوامر البيع', 'امر بيع', 'عرض سعر', 'عروض الأسعار',
                 'المبيعات', 'sales']
_KW_PRODUCT   = ['product', 'item', 'sku', 'best selling', 'best-selling',
                 'top selling', 'top product', 'منتج', 'المنتجات', 'صنف',
                 'الأصناف', 'الاصناف', 'أكثر المنتجات', 'اكثر المنتجات']
_KW_STOCK     = ['stock', 'inventory', 'on hand', 'on-hand', 'quantity available',
                 'warehouse', 'مخزون', 'المخزون', 'الكمية المتاحة', 'مستودع',
                 'المخازن', 'رصيد']
_KW_PURCHASE  = ['purchase', 'vendor', 'supplier', 'rfq', 'bill',
                 'مشتريات', 'المشتريات', 'مورد', 'الموردين', 'أمر شراء', 'امر شراء']
_KW_POS       = ['pos', 'point of sale', 'cashier', 'session', 'كاشير',
                 'نقطة البيع', 'نقاط البيع', 'الجلسة', 'الجلسات']
_KW_HR        = ['employee', 'headcount', 'department', 'payroll', 'payslip',
                 'attendance', 'موظف', 'الموظفين', 'الأقسام', 'الاقسام',
                 'الرواتب', 'راتب', 'قسيمة', 'الحضور']



def _contains(text, keywords):
    t = text.lower()
    return any(kw in t for kw in keywords)


ARABIC_DIGITS = {
    u'\u0660': '0', u'\u0661': '1', u'\u0662': '2', u'\u0663': '3', u'\u0664': '4',
    u'\u0665': '5', u'\u0666': '6', u'\u0667': '7', u'\u0668': '8', u'\u0669': '9',
    u'\u06f0': '0', u'\u06f1': '1', u'\u06f2': '2', u'\u06f3': '3', u'\u06f4': '4',
    u'\u06f5': '5', u'\u06f6': '6', u'\u06f7': '7', u'\u06f8': '8', u'\u06f9': '9',
}

# Month names -> number (English + Arabic, Levantine and Egyptian spellings)
MONTH_NAMES = {
    'january': 1, 'jan': 1, 'february': 2, 'feb': 2, 'march': 3, 'mar': 3,
    'april': 4, 'apr': 4, 'may': 5, 'june': 6, 'jun': 6, 'july': 7, 'jul': 7,
    'august': 8, 'aug': 8, 'september': 9, 'sep': 9, 'sept': 9,
    'october': 10, 'oct': 10, 'november': 11, 'nov': 11, 'december': 12, 'dec': 12,
    u'\u064a\u0646\u0627\u064a\u0631': 1, u'\u0643\u0627\u0646\u0648\u0646 \u0627\u0644\u062b\u0627\u0646\u064a': 1,
    u'\u0641\u0628\u0631\u0627\u064a\u0631': 2, u'\u0634\u0628\u0627\u0637': 2,
    u'\u0645\u0627\u0631\u0633': 3, u'\u0622\u0630\u0627\u0631': 3,
    u'\u0623\u0628\u0631\u064a\u0644': 4, u'\u0627\u0628\u0631\u064a\u0644': 4, u'\u0646\u064a\u0633\u0627\u0646': 4,
    u'\u0645\u0627\u064a\u0648': 5, u'\u0623\u064a\u0627\u0631': 5,
    u'\u064a\u0648\u0646\u064a\u0648': 6, u'\u062d\u0632\u064a\u0631\u0627\u0646': 6,
    u'\u064a\u0648\u0644\u064a\u0648': 7, u'\u062a\u0645\u0648\u0632': 7,
    u'\u0623\u063a\u0633\u0637\u0633': 8, u'\u0627\u063a\u0633\u0637\u0633': 8, u'\u0622\u0628': 8,
    u'\u0633\u0628\u062a\u0645\u0628\u0631': 9, u'\u0623\u064a\u0644\u0648\u0644': 9,
    u'\u0623\u0643\u062a\u0648\u0628\u0631': 10, u'\u0627\u0643\u062a\u0648\u0628\u0631': 10,
    u'\u0646\u0648\u0641\u0645\u0628\u0631': 11, u'\u062f\u064a\u0633\u0645\u0628\u0631': 12,
}


def _normalize(text):
    """Arabic-Indic digits -> ASCII, and strip tatweel/diacritics that break matching."""
    out = []
    for char in (text or ''):
        if char in ARABIC_DIGITS:
            out.append(ARABIC_DIGITS[char])
        elif char == u'\u0640':                       # tatweel
            continue
        elif u'\u064b' <= char <= u'\u0652':         # harakat
            continue
        else:
            out.append(char)
    return ''.join(out)


def _month_bounds(year, month):
    first = date(year, month, 1)
    if month == 12:
        last = date(year, 12, 31)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    return str(first), str(last)


def _extract_dates(text):
    """Understand a period written in English or Arabic (incl. Egyptian dialect).

    Returns (date_from, date_to) as YYYY-MM-DD strings. Falls back to the
    current month when nothing is recognised.
    """
    raw = _normalize(text or '')
    low = raw.lower()
    today = date.today()

    # ---- explicit ISO dates ------------------------------------------
    dates = re.findall(r'\d{4}-\d{2}-\d{2}', raw)
    if len(dates) >= 2:
        return dates[0], dates[1]
    if len(dates) == 1:
        return dates[0], str(today)

    # ---- day granularity ---------------------------------------------
    if re.search(u'\\btoday\\b|\u0627\u0644\u064a\u0648\u0645|\u0627\u0644\u0646\u0647\u0627\u0631\u062f\u0629|\u0627\u0644\u0646\u0647\u0627\u0631\u062f\u0647', low):
        return str(today), str(today)
    if re.search(u'\\byesterday\\b|\u0623\u0645\u0633|\u0627\u0645\u0633|\u0627\u0645\u0628\u0627\u0631\u062d|\u0625\u0645\u0628\u0627\u0631\u062d', low):
        yesterday = today - timedelta(days=1)
        return str(yesterday), str(yesterday)

    # ---- "last N days / months / years" ------------------------------
    span = re.search(
        u'(?:last|past|previous|\u0622\u062e\u0631|\u0627\u062e\u0631)\\s*(\\d{1,3})\\s*'
        u'(day|days|week|weeks|month|months|year|years|'
        u'\u064a\u0648\u0645|\u0623\u064a\u0627\u0645|\u0627\u064a\u0627\u0645|'
        u'\u0623\u0633\u0628\u0648\u0639|\u0623\u0633\u0627\u0628\u064a\u0639|\u0627\u0633\u0627\u0628\u064a\u0639|'
        u'\u0634\u0647\u0631|\u0634\u0647\u0648\u0631|\u0623\u0634\u0647\u0631|\u0627\u0634\u0647\u0631|'
        u'\u0633\u0646\u0629|\u0633\u0646\u0648\u0627\u062a|\u0639\u0627\u0645|\u0623\u0639\u0648\u0627\u0645)', low)
    if span:
        count = int(span.group(1))
        unit = span.group(2)
        if unit.startswith(('day', u'\u064a\u0648\u0645', u'\u0623\u064a\u0627\u0645', u'\u0627\u064a\u0627\u0645')):
            return str(today - timedelta(days=count)), str(today)
        if unit.startswith(('week', u'\u0623\u0633\u0628\u0648\u0639', u'\u0623\u0633\u0627\u0628\u064a\u0639', u'\u0627\u0633\u0627\u0628\u064a\u0639')):
            return str(today - timedelta(weeks=count)), str(today)
        if unit.startswith(('year', u'\u0633\u0646\u0629', u'\u0633\u0646\u0648\u0627\u062a', u'\u0639\u0627\u0645', u'\u0623\u0639\u0648\u0627\u0645')):
            return str(today.replace(year=today.year - count)), str(today)
        # months
        month = today.month - count
        year = today.year
        while month <= 0:
            month += 12
            year -= 1
        return str(date(year, month, 1)), str(today)

    # ---- week ---------------------------------------------------------
    if re.search(u'\\bthis week\\b|\u0647\u0630\u0627 \u0627\u0644\u0623\u0633\u0628\u0648\u0639|\u0627\u0644\u0623\u0633\u0628\u0648\u0639 \u0627\u0644\u062d\u0627\u0644\u064a|\u0627\u0644\u0627\u0633\u0628\u0648\u0639 \u062f\u0647', low):
        return str(today - timedelta(days=today.weekday())), str(today)
    if re.search(u'\\blast week\\b|\u0627\u0644\u0623\u0633\u0628\u0648\u0639 \u0627\u0644\u0645\u0627\u0636\u064a|\u0627\u0644\u0627\u0633\u0628\u0648\u0639 \u0627\u0644\u0645\u0627\u0636\u064a|\u0627\u0644\u0623\u0633\u0628\u0648\u0639 \u0627\u0644\u0644\u064a \u0641\u0627\u062a', low):
        start_this = today - timedelta(days=today.weekday())
        start_prev = start_this - timedelta(days=7)
        return str(start_prev), str(start_this - timedelta(days=1))

    # ---- month --------------------------------------------------------
    if re.search(u'\\bthis month\\b|\u0647\u0630\u0627 \u0627\u0644\u0634\u0647\u0631|\u0627\u0644\u0634\u0647\u0631 \u0627\u0644\u062d\u0627\u0644\u064a|\u0627\u0644\u0634\u0647\u0631 \u062f\u0647', low):
        return str(today.replace(day=1)), str(today)
    if re.search(u'\\blast month\\b|\u0627\u0644\u0634\u0647\u0631 \u0627\u0644\u0645\u0627\u0636\u064a|\u0627\u0644\u0634\u0647\u0631 \u0627\u0644\u0644\u064a \u0641\u0627\u062a|\u0627\u0644\u0634\u0647\u0631 \u0627\u0644\u0633\u0627\u0628\u0642', low):
        last_prev = today.replace(day=1) - timedelta(days=1)
        return _month_bounds(last_prev.year, last_prev.month)

    # ---- quarter ------------------------------------------------------
    quarter = (today.month - 1) // 3
    if re.search(u'\\bthis quarter\\b|\u0647\u0630\u0627 \u0627\u0644\u0631\u0628\u0639|\u0627\u0644\u0631\u0628\u0639 \u0627\u0644\u062d\u0627\u0644\u064a', low):
        return str(date(today.year, quarter * 3 + 1, 1)), str(today)
    if re.search(u'\\blast quarter\\b|\u0627\u0644\u0631\u0628\u0639 \u0627\u0644\u0645\u0627\u0636\u064a|\u0627\u0644\u0631\u0628\u0639 \u0627\u0644\u0633\u0627\u0628\u0642', low):
        year = today.year if quarter else today.year - 1
        prev_q = quarter - 1 if quarter else 3
        first = date(year, prev_q * 3 + 1, 1)
        last = date(year, prev_q * 3 + 3, 1)
        return str(first), _month_bounds(last.year, last.month)[1]
    q_match = re.search(u'\\bq([1-4])\\b|\u0627\u0644\u0631\u0628\u0639 \u0627\u0644(\u0623\u0648\u0644|\u0627\u0648\u0644|\u062b\u0627\u0646\u064a|\u062b\u0627\u0644\u062b|\u0631\u0627\u0628\u0639)', low)
    if q_match:
        arabic_order = {u'\u0623\u0648\u0644': 1, u'\u0627\u0648\u0644': 1, u'\u062b\u0627\u0646\u064a': 2,
                        u'\u062b\u0627\u0644\u062b': 3, u'\u0631\u0627\u0628\u0639': 4}
        num = int(q_match.group(1)) if q_match.group(1) else arabic_order.get(q_match.group(2), 1)
        year_match = re.search(r'\b(20\d{2})\b', raw)
        year = int(year_match.group(1)) if year_match else today.year
        first = date(year, (num - 1) * 3 + 1, 1)
        return str(first), _month_bounds(year, (num - 1) * 3 + 3)[1]

    # ---- year ---------------------------------------------------------
    if re.search(u'\\bthis year\\b|\u0647\u0630\u0627 \u0627\u0644\u0639\u0627\u0645|\u0647\u0630\u0647 \u0627\u0644\u0633\u0646\u0629|\u0627\u0644\u0633\u0646\u0629 \u0627\u0644\u062d\u0627\u0644\u064a\u0629|\u0627\u0644\u0633\u0646\u0629 \u062f\u064a', low):
        return str(today.replace(month=1, day=1)), str(today)
    if re.search(u'\\blast year\\b|\u0627\u0644\u0639\u0627\u0645 \u0627\u0644\u0645\u0627\u0636\u064a|\u0627\u0644\u0633\u0646\u0629 \u0627\u0644\u0645\u0627\u0636\u064a\u0629|\u0627\u0644\u0633\u0646\u0629 \u0627\u0644\u0644\u064a \u0641\u0627\u062a\u062a', low):
        return str(date(today.year - 1, 1, 1)), str(date(today.year - 1, 12, 31))

    # ---- month name (+ optional year) ---------------------------------
    for name, number in MONTH_NAMES.items():
        if name in low:
            year_match = re.search(r'\b(20\d{2})\b', raw)
            year = int(year_match.group(1)) if year_match else today.year
            return _month_bounds(year, number)

    # ---- bare year -----------------------------------------------------
    year_only = re.search(r'\b(20\d{2})\b', raw)
    if year_only:
        year = int(year_only.group(1))
        return str(date(year, 1, 1)), str(date(year, 12, 31))

    # ---- default: current month ----------------------------------------
    return str(today.replace(day=1)), str(today)


class InvoiceQueryEngine(models.AbstractModel):
    _name        = 'invoice.query.engine'
    _description = 'Pinned Report Engine (curated system reports)'

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_context_for_message(self, message: str) -> str:
        """Keyword-route a question to curated reports (no-AI fallback path)."""
        parts = []
        date_from, date_to = _extract_dates(message)

        # -- non-invoice domains first: they are more specific -------------
        if _contains(message, _KW_STOCK):
            parts.append(self._report_stock_on_hand())
        if _contains(message, _KW_PURCHASE):
            parts.append(self._report_purchase_summary(date_from, date_to))
        if _contains(message, _KW_POS):
            parts.append(self._report_pos_summary(date_from, date_to))
        if _contains(message, _KW_PRODUCT):
            parts.append(self._report_top_products(date_from, date_to))
        if _contains(message, _KW_HR):
            parts.append(self._report_hr_headcount())
        if _contains(message, _KW_SALES_ORD) and not parts:
            parts.append(self._report_sales_orders(date_from, date_to))

        other_domain = bool(parts)

        # -- invoice / accounting domain ------------------------------------
        if _contains(message, _KW_OVERDUE):
            parts.append(self._report_overdue())

        if _contains(message, _KW_BRANCH):
            parts.append(self._report_by_branch_journal(date_from, date_to))

        if _contains(message, _KW_INV_TYPE) and not other_domain:
            parts.append(self._report_by_invoice_type(date_from, date_to))

        if _contains(message, _KW_TOP_CUST) and not other_domain:
            parts.append(self._report_top_customers(date_from, date_to))

        if (_contains(message, _KW_PAID) or _contains(message, _KW_SUMMARY)) \
                and not other_domain:
            parts.append(self._report_paid_summary(date_from, date_to))

        if _contains(message, _KW_DATE) and not parts:
            parts.append(self._report_date_range(date_from, date_to))

        if not parts:
            # Default: general invoice summary
            parts.append(self._report_general_summary(date_from, date_to))

        return '\n\n'.join(p for p in parts if p)

    # ------------------------------------------------------------------
    # Pinned report catalog -- exposed to the AI as the `run_report` tool
    # ------------------------------------------------------------------
    def list_reports(self):
        """Return the catalog as text for the AI system prompt."""
        lines = ['=== Pinned reports (call run_report with these names) ===']
        for name, meta in REPORT_CATALOG.items():
            dates = ' [accepts date_from/date_to]' if meta['dates'] else ''
            lines.append('  %-28s %s%s' % (name, meta['label'], dates))
        return '\n'.join(lines)

    def run_report(self, name, date_from=None, date_to=None, limit=15):
        """Run one curated report by name."""
        meta = REPORT_CATALOG.get(name)
        if not meta:
            return ("Unknown report '%s'. Available: %s"
                    % (name, ', '.join(REPORT_CATALOG)))
        today = date.today()
        if meta['dates']:
            date_from = date_from or str(today.replace(day=1))
            date_to = date_to or str(today)
        method = getattr(self, meta['method'], None)
        if method is None:
            return "Report '%s' is not implemented." % name
        try:
            if meta['dates'] and meta.get('limit'):
                return method(date_from, date_to, limit)
            if meta['dates']:
                return method(date_from, date_to)
            if meta.get('limit'):
                return method(limit)
            return method()
        except Exception as exc:                            # pragma: no cover
            _logger.warning('Pinned report %s failed: %s', name, exc)
            return "Report '%s' failed: %s" % (name, exc)

    # ------------------------------------------------------------------
    # Report builders
    # ------------------------------------------------------------------

    def _report_general_summary(self, date_from, date_to):
        cr = self.env.cr
        cr.execute("""
            SELECT
                COUNT(*)                                           AS total_invoices,
                SUM(amount_total_signed)                           AS total_amount,
                SUM(CASE WHEN amount_residual_signed <= 1
                         THEN amount_total_signed ELSE 0 END)      AS paid_amount,
                SUM(CASE WHEN amount_residual_signed > 1
                         THEN amount_residual_signed ELSE 0 END)   AS outstanding_amount,
                COUNT(CASE WHEN amount_residual_signed > 1
                           THEN 1 END)                             AS open_count
            FROM account_move
            WHERE move_type    = 'out_invoice'
              AND state        = 'posted'
              AND invoice_date >= %s
              AND invoice_date <= %s
        """, (date_from, date_to))
        row = cr.fetchone()
        if not row:
            return 'No invoices found between ' + date_from + ' and ' + date_to + '.'

        total_inv, total_amt, paid_amt, outs_amt, open_cnt = row
        return (
            '=== General Invoice Summary (' + date_from + ' -> ' + date_to + ') ===\n'
            'Total Invoices   : ' + str(total_inv or 0) + '\n'
            'Total Amount     : ' + '{:,.2f}'.format(float(total_amt or 0)) + '\n'
            'Paid Amount      : ' + '{:,.2f}'.format(float(paid_amt or 0)) + '\n'
            'Outstanding Amt  : ' + '{:,.2f}'.format(float(outs_amt or 0)) + '\n'
            'Open Invoices    : ' + str(open_cnt or 0) + '\n'
        )

    def _report_overdue(self):
        cr = self.env.cr
        today = str(date.today())
        cr.execute("""
            SELECT
                am.name,
                rp.name  AS customer,
                rb.name  AS branch,
                am.invoice_date_due,
                am.amount_residual_signed
            FROM account_move am
            LEFT JOIN res_partner rp ON rp.id = am.partner_id
            LEFT JOIN res_branch  rb ON rb.id = am.branch_id
            WHERE am.move_type           = 'out_invoice'
              AND am.state               = 'posted'
              AND am.amount_residual_signed > 1
              AND am.invoice_date_due    < %s
            ORDER BY am.invoice_date_due ASC
            LIMIT 50
        """, (today,))
        rows = cr.fetchall()
        if not rows:
            return "=== Overdue Invoices ===\nNo overdue invoices found."

        lines = ["=== Overdue Invoices (top 50) ===",
                 '{:<20} {:<30} {:<20} {:<12} {:>12}'.format(
                     'Invoice', 'Customer', 'Branch', 'Due Date', 'Balance')]
        lines.append('-' * 100)
        for name, customer, branch, due, balance in rows:
            lines.append(
                '{:<20} {:<30} {:<20} {:<12} {:>12,.2f}'.format(
                    str(name), str(customer), str(branch),
                    str(due), float(balance or 0))
            )
        return '\n'.join(lines)

    # ------------------------------------------------------------------
    # Payment lines — ORM-based, mirrors branch_account_report wizard
    # ------------------------------------------------------------------

    def _get_payment_lines(self, date_from, date_to):
        """Return list of (journal_name, branch_name, amount, source_type) tuples."""
        ctx = {'allowed_company_ids': self.env.companies.ids}
        AccountMove    = self.env['account.move'].with_context(**ctx)
        AccountPayment = self.env['account.payment'].with_context(**ctx)

        invoices = AccountMove.search([
            ('move_type',    '=',  'out_invoice'),
            ('state',        '=',  'posted'),
            ('invoice_date', '>=', date_from),
            ('invoice_date', '<=', date_to),
        ])
        credits = AccountMove.search([
            ('move_type',    '=',  'out_refund'),
            ('state',        '=',  'posted'),
            ('invoice_date', '>=', date_from),
            ('invoice_date', '<=', date_to),
        ])
        order_payments = AccountPayment.search([
            ('sale_order_id', '!=', False),
            ('state',         '=',  'paid'),
            ('date',          '>=', date_from),
            ('date',          '<=', date_to),
        ])

        lines = []

        # ---- invoices ------------------------------------------------
        for inv in invoices:
            branch_name = inv.branch_id.name or ''
            payments    = inv._get_reconciled_payments()
            if payments:
                for pmt in payments:
                    lines.append((
                        pmt.journal_id.name or '',
                        branch_name,
                        pmt.amount,
                        pmt.journal_id.payment_type,
                    ))
            else:
                for pmt in inv.pos_order_ids.payment_ids:
                    lines.append((
                        pmt.payment_method_id.journal_id.name or '',
                        branch_name,
                        pmt.amount,
                        pmt.payment_method_id.journal_id.payment_type,
                    ))

        # ---- credit notes --------------------------------------------
        for inv in credits:
            branch_name = inv.branch_id.name or ''
            payments    = inv._get_reconciled_payments()
            if payments:
                for pmt in payments:
                    lines.append((
                        pmt.journal_id.name or '',
                        branch_name,
                        -pmt.amount,
                        pmt.journal_id.payment_type,
                    ))
            else:
                for pmt in inv.pos_order_ids.payment_ids:
                    lines.append((
                        pmt.payment_method_id.journal_id.name or '',
                        branch_name,
                        pmt.amount,
                        pmt.payment_method_id.journal_id.payment_type,
                    ))

        # ---- sale-order payments -------------------------------------
        for pmt in order_payments:
            signed = -pmt.amount if pmt.payment_type == 'outbound' else pmt.amount
            lines.append((
                pmt.journal_id.name or '',
                pmt.branch_id.name or '',
                signed,
                pmt.journal_id.payment_type,
            ))

        return lines

    def _report_by_branch_journal(self, date_from, date_to):
        """Matrix report: rows = journals, columns = branches, cells = summed amounts."""
        raw = self._get_payment_lines(date_from, date_to)
        if not raw:
            return (
                '=== Payments by Branch & Journal (' + date_from + ' -> ' + date_to + ') ===\n'
                'No payment data found.'
            )

        matrix = {}
        journals_seen = []
        branches_seen = []
        j_set = set()
        b_set = set()
        pay_type = {}

        for journal, branch, amount, ptype in raw:
            if not journal:
                journal = 'Other'
            if not branch:
                branch = 'Other'
            if journal not in j_set:
                j_set.add(journal)
                journals_seen.append(journal)
            if branch not in b_set:
                b_set.add(branch)
                branches_seen.append(branch)
            key = (journal, branch)
            matrix[key] = matrix.get(key, 0.0) + float(amount or 0)
            if journal not in pay_type:
                pay_type[journal] = ptype or ''

        journals_seen.sort()
        branches_seen.sort()

        j_w   = max(20, max(len(j) for j in journals_seen) + 2)
        num_w = max(14, max(len(b) for b in branches_seen) + 2)

        header = '{:<{}}'.format('Journal', j_w)
        for b in branches_seen:
            header += '{:>{}}'.format(b, num_w)
        header += '{:>{}}'.format('Total', num_w)
        sep = '-' * len(header)

        lines = [
            '=== Payments by Branch & Journal (' + date_from + ' -> ' + date_to + ') ===',
            header, sep,
        ]

        grand_total = 0.0
        branch_totals = {b: 0.0 for b in branches_seen}

        for journal in journals_seen:
            row_total = 0.0
            row = '{:<{}}'.format(journal, j_w)
            for branch in branches_seen:
                amt = matrix.get((journal, branch), 0.0)
                row_total            += amt
                branch_totals[branch] += amt
                row += '{:>{},.2f}'.format(amt, num_w)
            grand_total += row_total
            row += '{:>{},.2f}'.format(row_total, num_w)
            lines.append(row)

        lines.append(sep)
        footer = '{:<{}}'.format('Total', j_w)
        for branch in branches_seen:
            footer += '{:>{},.2f}'.format(branch_totals[branch], num_w)
        footer += '{:>{},.2f}'.format(grand_total, num_w)
        lines.append(footer)

        type_summary = {}
        for journal, branch, amount, ptype in raw:
            label = ptype or 'other'
            type_summary[label] = type_summary.get(label, 0.0) + float(amount or 0)

        if type_summary:
            lines.append('')
            lines.append('--- Payment Type Breakdown ---')
            for ptype, total in sorted(type_summary.items()):
                lines.append('  {:<20} {:>15,.2f}'.format(ptype, total))

        return '\n'.join(lines)

    def _report_by_invoice_type(self, date_from, date_to):
        """Breakdown of invoices and credit notes by type, with subtotals."""
        cr = self.env.cr
        cr.execute("""
            SELECT
                move_type,
                COUNT(*)                                               AS cnt,
                SUM(amount_total_signed)                               AS gross,
                SUM(CASE WHEN amount_residual_signed <= 1
                         THEN amount_total_signed ELSE 0 END)          AS paid,
                SUM(CASE WHEN amount_residual_signed > 1
                         THEN amount_residual_signed ELSE 0 END)       AS outstanding,
                COUNT(CASE WHEN amount_residual_signed > 1 THEN 1 END) AS open_cnt
            FROM account_move
            WHERE move_type    IN ('out_invoice', 'out_refund')
              AND state        = 'posted'
              AND invoice_date >= %s
              AND invoice_date <= %s
            GROUP BY move_type
            ORDER BY move_type
        """, (date_from, date_to))
        rows = cr.fetchall()
        if not rows:
            return (
                '=== Invoice Analysis by Type (' + date_from + ' -> ' + date_to + ') ===\n'
                'No documents found.'
            )

        type_labels = {
            'out_invoice': 'Sales Invoice',
            'out_refund':  'Credit Note',
        }
        col_w = 18
        lines = [
            '=== Invoice Analysis by Type (' + date_from + ' -> ' + date_to + ') ===',
            '{:<20} {:>{}} {:>{}} {:>{}} {:>{}} {:>{}}'.format(
                'Type', 'Count', col_w, 'Gross Amount', col_w,
                'Paid', col_w, 'Outstanding', col_w, 'Open #', col_w),
            '-' * (20 + col_w * 5),
        ]
        tot_cnt = tot_gross = tot_paid = tot_outs = tot_open = 0
        for move_type, cnt, gross, paid, outstanding, open_cnt in rows:
            label = type_labels.get(move_type, move_type)
            c  = int(cnt or 0)
            g  = float(gross or 0)
            p  = float(paid or 0)
            o  = float(outstanding or 0)
            oc = int(open_cnt or 0)
            tot_cnt  += c
            tot_gross += g
            tot_paid  += p
            tot_outs  += o
            tot_open  += oc
            lines.append(
                '{:<20} {:>{},} {:>{},.2f} {:>{},.2f} {:>{},.2f} {:>{},}'.format(
                    label, c, col_w, g, col_w, p, col_w, o, col_w, oc, col_w)
            )
        lines.append('-' * (20 + col_w * 5))
        lines.append(
            '{:<20} {:>{},} {:>{},.2f} {:>{},.2f} {:>{},.2f} {:>{},}'.format(
                'TOTAL', tot_cnt, col_w, tot_gross, col_w,
                tot_paid, col_w, tot_outs, col_w, tot_open, col_w)
        )
        if tot_gross:
            rate = (tot_paid / tot_gross) * 100
            lines.append('\nCollection Rate : {:.1f}%'.format(rate))
        return '\n'.join(lines)

    def _report_top_customers(self, date_from, date_to, limit=15):
        """Rank customers by total invoiced amount in the period."""
        cr = self.env.cr
        cr.execute("""
            SELECT
                rp.name                                                AS customer,
                rb.name                                                AS branch,
                COUNT(*)                                               AS invoices,
                SUM(am.amount_total_signed)                            AS total_invoiced,
                SUM(CASE WHEN am.amount_residual_signed <= 1
                         THEN am.amount_total_signed ELSE 0 END)       AS total_paid,
                SUM(CASE WHEN am.amount_residual_signed > 1
                         THEN am.amount_residual_signed ELSE 0 END)    AS outstanding
            FROM account_move am
            LEFT JOIN res_partner rp ON rp.id = am.partner_id
            LEFT JOIN res_branch  rb ON rb.id = am.branch_id
            WHERE am.move_type    = 'out_invoice'
              AND am.state        = 'posted'
              AND am.invoice_date >= %s
              AND am.invoice_date <= %s
            GROUP BY rp.name, rb.name
            ORDER BY total_invoiced DESC
            LIMIT %s
        """, (date_from, date_to, limit))
        rows = cr.fetchall()
        if not rows:
            return (
                '=== Top Customers by Sales (' + date_from + ' -> ' + date_to + ') ===\n'
                'No data found.'
            )

        lines = [
            '=== Top ' + str(limit) + ' Customers by Sales (' + date_from + ' -> ' + date_to + ') ===',
            '{:<4} {:<30} {:<20} {:>8} {:>15} {:>15} {:>13}'.format(
                '#', 'Customer', 'Branch', 'Invoices', 'Invoiced', 'Paid', 'Outstanding'),
            '-' * 110,
        ]
        for rank, (customer, branch, inv_cnt, invoiced, paid, outstanding) in enumerate(rows, 1):
            lines.append(
                '{:<4} {:<30} {:<20} {:>8,} {:>15,.2f} {:>15,.2f} {:>13,.2f}'.format(
                    rank,
                    str(customer or '-')[:30],
                    str(branch or '-')[:20],
                    int(inv_cnt or 0),
                    float(invoiced or 0),
                    float(paid or 0),
                    float(outstanding or 0),
                )
            )

        total_inv  = sum(float(r[3] or 0) for r in rows)
        total_paid = sum(float(r[4] or 0) for r in rows)
        total_outs = sum(float(r[5] or 0) for r in rows)
        lines.append('-' * 110)
        lines.append(
            '{:<54} {:>15,.2f} {:>15,.2f} {:>13,.2f}'.format(
                'Top ' + str(len(rows)) + ' Subtotal',
                total_inv, total_paid, total_outs)
        )
        return '\n'.join(lines)

    def _report_paid_summary(self, date_from, date_to):
        cr = self.env.cr
        cr.execute("""
            SELECT
                rb.name AS branch,
                SUM(am.amount_total_signed) AS total
            FROM account_move am
            LEFT JOIN res_branch rb ON rb.id = am.branch_id
            WHERE am.move_type           IN ('out_invoice', 'out_refund')
              AND am.state               = 'posted'
              AND am.amount_residual_signed <= 1
              AND am.invoice_date        >= %s
              AND am.invoice_date        <= %s
            GROUP BY rb.name
            ORDER BY total DESC
        """, (date_from, date_to))
        rows = cr.fetchall()
        if not rows:
            return (
                '=== Paid Invoice Summary (' + date_from + ' -> ' + date_to + ') ===\n'
                'No paid invoices found.'
            )

        lines = [
            '=== Paid Invoice Summary by Branch (' + date_from + ' -> ' + date_to + ') ===',
            '{:<30} {:>15}'.format('Branch', 'Total Paid'),
            '-' * 50,
        ]
        grand = 0
        for branch, total in rows:
            t = float(total or 0)
            grand += t
            lines.append('{:<30} {:>15,.2f}'.format(str(branch), t))
        lines.append('-' * 50)
        lines.append('{:<30} {:>15,.2f}'.format('Grand Total', grand))
        return '\n'.join(lines)

    def _report_date_range(self, date_from, date_to):
        cr = self.env.cr
        cr.execute("""
            SELECT
                am.move_type,
                COUNT(*)                   AS cnt,
                SUM(am.amount_total_signed) AS total,
                SUM(am.amount_residual_signed) AS outstanding
            FROM account_move am
            WHERE am.move_type  IN ('out_invoice', 'out_refund')
              AND am.state       = 'posted'
              AND am.invoice_date >= %s
              AND am.invoice_date <= %s
            GROUP BY am.move_type
        """, (date_from, date_to))
        rows = cr.fetchall()
        if not rows:
            return (
                '=== Date Range Report (' + date_from + ' -> ' + date_to + ') ===\n'
                'No documents found.'
            )

        lines = [
            '=== Invoice Analytics (' + date_from + ' -> ' + date_to + ') ===',
            '{:<20} {:>8} {:>15} {:>15}'.format('Type', 'Count', 'Total', 'Outstanding'),
            '-' * 65,
        ]
        for move_type, cnt, total, outstanding in rows:
            label = 'Invoice' if move_type == 'out_invoice' else 'Credit Note'
            lines.append(
                '{:<20} {:>8} {:>15,.2f} {:>15,.2f}'.format(
                    label, int(cnt or 0),
                    float(total or 0), float(outstanding or 0))
            )
        return '\n'.join(lines)

    # ------------------------------------------------------------------
    # System-wide report builders (backed by the generic query engine, so
    # they automatically respect the current user's access rights)
    # ------------------------------------------------------------------

    def _engine(self):
        return self.env['ai.data.query.engine']

    def _unavailable(self, model_name, label):
        return ('=== %s ===\n'
                "'%s' is not available in this database "
                '(module not installed, or you have no access to it).'
                % (label, model_name))

    def _report_sales_orders(self, date_from, date_to):
        """Sales orders / quotations grouped by status for the period."""
        if 'sale.order' not in self.env:
            return self._unavailable('sale.order', 'Sales Orders')
        body = self._engine().quick_group(
            'sale.order',
            [('date_order', '>=', date_from + ' 00:00:00'),
             ('date_order', '<=', date_to + ' 23:59:59')],
            ['state'],
            ['amount_untaxed:sum', 'amount_total:sum'],
            order='amount_total desc',
            limit=20,
        )
        return ('=== Sales Orders (' + date_from + ' -> ' + date_to + ') ===\n' + body)

    def _report_top_products(self, date_from, date_to, limit=15):
        """Best-selling products for the period (confirmed sales orders)."""
        if 'sale.order.line' not in self.env:
            return self._unavailable('sale.order.line', 'Top Products')
        body = self._engine().quick_group(
            'sale.order.line',
            [('order_id.state', 'in', ['sale', 'done']),
             ('order_id.date_order', '>=', date_from + ' 00:00:00'),
             ('order_id.date_order', '<=', date_to + ' 23:59:59')],
            ['product_id'],
            ['product_uom_qty:sum', 'price_subtotal:sum'],
            order='price_subtotal desc',
            limit=limit,
        )
        return ('=== Top Products by Sales (' + date_from + ' -> ' + date_to + ') ===\n'
                + body)

    def _report_stock_on_hand(self, limit=20):
        """On-hand quantity per product in internal locations."""
        if 'stock.quant' not in self.env:
            return self._unavailable('stock.quant', 'Stock On Hand')
        body = self._engine().quick_group(
            'stock.quant',
            [('location_id.usage', '=', 'internal')],
            ['product_id'],
            ['quantity:sum'],
            order='quantity desc',
            limit=limit,
        )
        return '=== Stock On Hand (internal locations) ===\n' + body

    def _report_purchase_summary(self, date_from, date_to):
        """Purchase orders grouped by status for the period."""
        if 'purchase.order' not in self.env:
            return self._unavailable('purchase.order', 'Purchases')
        body = self._engine().quick_group(
            'purchase.order',
            [('date_order', '>=', date_from + ' 00:00:00'),
             ('date_order', '<=', date_to + ' 23:59:59')],
            ['state'],
            ['amount_untaxed:sum', 'amount_total:sum'],
            order='amount_total desc',
            limit=20,
        )
        return ('=== Purchase Orders (' + date_from + ' -> ' + date_to + ') ===\n' + body)

    def _report_pos_summary(self, date_from, date_to):
        """POS orders grouped by session for the period."""
        if 'pos.order' not in self.env:
            return self._unavailable('pos.order', 'POS Sales')
        body = self._engine().quick_group(
            'pos.order',
            [('date_order', '>=', date_from + ' 00:00:00'),
             ('date_order', '<=', date_to + ' 23:59:59'),
             ('state', 'in', ['paid', 'done', 'invoiced'])],
            ['session_id'],
            ['amount_total:sum', 'amount_paid:sum'],
            order='amount_total desc',
            limit=25,
        )
        return ('=== POS Sales by Session (' + date_from + ' -> ' + date_to + ') ===\n'
                + body)

    def _report_hr_headcount(self):
        """Employee headcount per department."""
        if 'hr.employee' not in self.env:
            return self._unavailable('hr.employee', 'Headcount')
        body = self._engine().quick_group(
            'hr.employee',
            [],
            ['department_id'],
            [],
            order=None,
            limit=50,
        )
        return '=== Headcount by Department ===\n' + body


# ---------------------------------------------------------------------------
# Catalog of pinned reports exposed to the AI (`run_report` tool) and used by
# the keyword fallback.  'dates' -> method takes (date_from, date_to);
# 'limit' -> method also takes a row limit.
# ---------------------------------------------------------------------------
REPORT_CATALOG = {
    'invoice_summary': {
        'label': 'Sales invoices: totals, paid, outstanding',
        'method': '_report_general_summary', 'dates': True, 'limit': False,
    },
    'invoice_by_type': {
        'label': 'Invoices vs credit notes, with collection rate',
        'method': '_report_by_invoice_type', 'dates': True, 'limit': False,
    },
    'overdue_invoices': {
        'label': 'Overdue customer invoices (top 50, no date filter)',
        'method': '_report_overdue', 'dates': False, 'limit': False,
    },
    'payments_by_branch_journal': {
        'label': 'Payment matrix: journals x branches, incl. POS payments',
        'method': '_report_by_branch_journal', 'dates': True, 'limit': False,
    },
    'paid_by_branch': {
        'label': 'Paid invoice totals per branch',
        'method': '_report_paid_summary', 'dates': True, 'limit': False,
    },
    'invoice_date_range': {
        'label': 'Invoice/credit-note counts and totals for a period',
        'method': '_report_date_range', 'dates': True, 'limit': False,
    },
    'top_customers': {
        'label': 'Customer ranking by invoiced amount',
        'method': '_report_top_customers', 'dates': True, 'limit': True,
    },
    'sales_orders_summary': {
        'label': 'Sales orders / quotations by status',
        'method': '_report_sales_orders', 'dates': True, 'limit': False,
    },
    'top_products': {
        'label': 'Best-selling products (confirmed sales orders)',
        'method': '_report_top_products', 'dates': True, 'limit': True,
    },
    'stock_on_hand': {
        'label': 'On-hand quantity per product (internal locations)',
        'method': '_report_stock_on_hand', 'dates': False, 'limit': True,
    },
    'purchase_summary': {
        'label': 'Purchase orders by status',
        'method': '_report_purchase_summary', 'dates': True, 'limit': False,
    },
    'pos_summary': {
        'label': 'POS sales grouped by session',
        'method': '_report_pos_summary', 'dates': True, 'limit': False,
    },
    'hr_headcount': {
        'label': 'Employee headcount per department',
        'method': '_report_hr_headcount', 'dates': False, 'limit': False,
    },
}

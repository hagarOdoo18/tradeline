# -*- coding: utf-8 -*-
from odoo import fields, models
import xlsxwriter
from io import BytesIO
import base64
from datetime import datetime
from itertools import groupby as igrp


class AccountInvoiceAccountingWizard(models.TransientModel):
    _name        = 'account.invoice.accounting.wizard'
    _description = 'Accounting Excel Report Wizard'

    excel_file = fields.Binary(string='Download Report Excel', readonly=True)
    file_name  = fields.Char(string='Excel File', size=64)
    date_from  = fields.Date(string='Date From', required=True)
    date_to    = fields.Date(string='Date To',   required=True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _s(self, val):
        """Return a plain string from val.
        Odoo 18 stores translatable Char fields as JSONB, so psycopg2 may
        return a dict like {'en_US': 'Cash', 'ar_001': 'نقدا'}.
        This helper unwraps dicts and falls back gracefully.
        """
        if isinstance(val, dict):
            # prefer current user language, then first available value
            lang = self.env.lang or 'en_US'
            return str(val.get(lang) or next(iter(val.values()), '') or '')
        return str(val or '')

    # ------------------------------------------------------------------
    # SQL helpers  (Odoo 18 table / column names)
    # ------------------------------------------------------------------

    def _get_sql_open_invoice(self, date_from, date_to):
        return """
            SELECT am.name, rb.name, rp.name, am.amount_total_signed, am.invoice_date
            FROM account_move am
            LEFT JOIN res_branch rb ON rb.id = am.branch_id
            LEFT JOIN res_partner rp ON rp.id = am.partner_id
            WHERE am.invoice_date >= '{df}'
              AND am.invoice_date <= '{dt}'
              AND am.state = 'posted'
              AND am.move_type = 'out_invoice'
              AND am.amount_residual_signed > 1
            ORDER BY rb.name, am.invoice_date
        """.format(df=date_from, dt=date_to)

    def _get_sql_open_credit(self, date_from, date_to):
        return """
            SELECT am.name, rb.name, rp.name, am.amount_total_signed, am.invoice_date
            FROM account_move am
            LEFT JOIN res_branch rb ON rb.id = am.branch_id
            LEFT JOIN res_partner rp ON rp.id = am.partner_id
            WHERE am.invoice_date >= '{df}'
              AND am.invoice_date <= '{dt}'
              AND am.state = 'posted'
              AND am.move_type = 'out_refund'
              AND am.amount_residual_signed < -1
            ORDER BY rb.name, am.invoice_date
        """.format(df=date_from, dt=date_to)

    def _get_sql_total_paid_invoice(self, date_from, date_to):
        return """
            SELECT rb.name, SUM(am.amount_total_signed)
            FROM account_move am
            LEFT JOIN res_branch rb ON rb.id = am.branch_id
            WHERE am.invoice_date >= '{df}'
              AND am.invoice_date <= '{dt}'
              AND am.state = 'posted'
              AND am.move_type = 'out_invoice'
              AND am.amount_residual_signed <= 1
            GROUP BY rb.name
            ORDER BY rb.name
        """.format(df=date_from, dt=date_to)

    def _get_sql_total_paid_credit(self, date_from, date_to):
        return """
            SELECT rb.name, SUM(am.amount_total_signed)
            FROM account_move am
            LEFT JOIN res_branch rb ON rb.id = am.branch_id
            WHERE am.invoice_date >= '{df}'
              AND am.invoice_date <= '{dt}'
              AND am.state = 'posted'
              AND am.move_type = 'out_refund'
              AND am.amount_residual_signed >= -1
            GROUP BY rb.name
            ORDER BY rb.name
        """.format(df=date_from, dt=date_to)

    def _get_sql_total_sro(self, date_from, date_to):
        return """
            SELECT rb.name, SUM(so.amount_total)
            FROM sale_order so
            LEFT JOIN res_branch rb ON rb.id = so.branch_id
            WHERE so.date_order::date >= '{df}'
              AND so.date_order::date <= '{dt}'
              AND so.state = 'sale'
              AND so.inv_type = 'sro'
            GROUP BY rb.name
            ORDER BY rb.name
        """.format(df=date_from, dt=date_to)

    def _get_payment_lines(self, date_from, date_to):
        """Return list of (journal_name, branch_name, amount, source_type) tuples.

        ORM-based implementation that mirrors the logic of
        ``action_view_payment_report`` in the branch_account_report wizard:
          * invoices  -> reconciled account.payment, else POS payments
          * credits   -> reconciled account.payment (negated), else POS payments
          * sale-order payments (signed by payment_type)
        """
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

        lines.sort(key=lambda l: (str(l[3] or '').casefold(), str(l[1] or '').casefold()))
        return lines

    # ------------------------------------------------------------------
    # Main action
    # ------------------------------------------------------------------

    def print_excel(self):
        cr = self.env.cr
        df, dt = self.date_from, self.date_to

        cr.execute(self._get_sql_open_invoice(df, dt))
        open_invoices = cr.fetchall()

        cr.execute(self._get_sql_open_credit(df, dt))
        open_credit = cr.fetchall()

        workbook, output, filename = self._generate_excel_open(
            open_invoices, open_credit)

        cr.execute(self._get_sql_total_paid_invoice(df, dt))
        paid_invoices = cr.fetchall()

        cr.execute(self._get_sql_total_paid_credit(df, dt))
        paid_credits = cr.fetchall()

        workbook = self._generate_excel_paid(paid_invoices, paid_credits, workbook)

        cr.execute(self._get_sql_total_sro(df, dt))
        sro_orders = cr.fetchall()

        workbook = self._generate_excel_sro(sro_orders, workbook)

        payments = self._get_payment_lines(df, dt)

        workbook = self._generate_excel_payment(payments, workbook)
        workbook = self._generate_excel_type_payment(payments, workbook)
        workbook = self._generate_excel_all(
            open_invoices, open_credit,
            paid_invoices, paid_credits,
            sro_orders, payments, workbook)

        workbook.close()
        output.seek(0)

        self.file_name  = filename + datetime.today().strftime('%Y-%m-%d') + '.xlsx'
        self.excel_file = base64.b64encode(output.read())

        return {
            'type':      'ir.actions.act_window',
            'res_model': 'account.invoice.accounting.wizard',
            'res_id':    self.id,
            'view_mode': 'form',
            'target':    'new',
        }

    # ------------------------------------------------------------------
    # Excel sheet builders
    # ------------------------------------------------------------------

    def _fmt(self, wb):
        """Return (bold_fmt, cell_fmt, header_fmt) for a workbook."""
        bold = wb.add_format({
            'bold': 1, 'border': 1, 'align': 'center',
            'valign': 'vcenter', 'text_wrap': True, 'font_size': 11,
        })
        cell = wb.add_format({
            'font_name': 'KacstBook', 'font_size': 10,
            'align': 'center', 'valign': 'vcenter',
            'text_wrap': True, 'border': 1,
        })
        header = wb.add_format({
            'bold': 1, 'border': 1, 'bg_color': '#AAB7B8',
            'font_size': 10, 'align': 'center',
            'valign': 'vcenter', 'text_wrap': True,
        })
        return bold, cell, header

    def _generate_excel_open(self, lines, credit):
        filename = 'Accounting Report '
        output   = BytesIO()
        workbook = xlsxwriter.Workbook(output, {'in_memory': True})
        sheet    = workbook.add_worksheet('Open Invoices')
        bold, cell, header = self._fmt(workbook)

        sheet.set_column(0, 0, 10)
        sheet.set_column(1, 3, 20)
        for col, title in enumerate(['Number', 'Branch', 'Customer', 'Total']):
            sheet.write(0, col, title, header)

        row = 1
        for line in list(lines) + list(credit):
            sheet.write(row, 0, self._s(line[0]), cell)
            sheet.write(row, 1, self._s(line[1]), cell)
            sheet.write(row, 2, self._s(line[2]), cell)
            sheet.write(row, 3, float(line[3] or 0), cell)
            sheet.set_row(row, 40)
            row += 1

        return workbook, output, filename

    def _generate_excel_paid(self, lines, credits, workbook):
        sheet = workbook.add_worksheet('Paid Invoices')
        bold, cell, header = self._fmt(workbook)

        sheet.set_column(0, 0, 30)
        sheet.write(0, 0, 'Branch', header)
        sheet.write(1, 0, 'Total',  header)

        store = {}
        for line in lines:
            store[self._s(line[0])] = float(line[1] or 0)
        for line in credits:
            key = self._s(line[0])
            store[key] = store.get(key, 0) + float(line[1] or 0)

        col = 1
        total = 0
        for key, value in sorted(store.items(), key=lambda x: x[0].casefold()):
            sheet.set_column(0, col, 30)
            sheet.write(0, col, key, header)
            sheet.write(1, col, value, cell)
            total += value
            col   += 1
        sheet.set_row(0, 40)
        sheet.set_row(1, 40)
        sheet.write(1, col, total, header)
        return workbook

    def _generate_excel_sro(self, lines, workbook):
        sheet = workbook.add_worksheet('SRO')
        bold, cell, header = self._fmt(workbook)

        sheet.set_column(0, 0, 30)
        sheet.write(0, 0, 'Branch', header)
        sheet.write(1, 0, 'Total',  header)

        col = 1
        total = 0
        for line in lines:
            sheet.set_column(0, col, 30)
            sheet.write(0, col, self._s(line[0]), header)
            sheet.write(1, col, float(line[1] or 0), cell)
            total += float(line[1] or 0)
            col   += 1
        sheet.set_row(0, 40)
        sheet.set_row(1, 40)
        sheet.write(1, col, total, header)
        return workbook

    def _generate_excel_payment(self, lines, workbook):
        """Matrix sheet: rows = payment journals, cols = branches, cells = SUM.

        Aggregates every (journal, branch) pair so duplicates are added
        together rather than overwriting. Adds a Total row (per branch) and
        a Total column (per journal), plus a grand total in the corner.
        """
        sheet = workbook.add_worksheet('Branches Payments')
        bold, cell, header = self._fmt(workbook)

        # Normalise to plain strings/floats
        norm = [(self._s(l[0]), self._s(l[1]), float(l[2] or 0), self._s(l[3]))
                for l in lines]

        # Build unique journal/branch axes and (journal, branch) -> sum map
        matrix     = {}
        journals   = []
        branches   = []
        j_seen     = set()
        b_seen     = set()
        for journal, branch, amount, _src in norm:
            if not journal or not branch:
                continue
            if journal not in j_seen:
                j_seen.add(journal)
                journals.append(journal)
            if branch not in b_seen:
                b_seen.add(branch)
                branches.append(branch)
            key = (journal, branch)
            matrix[key] = matrix.get(key, 0) + amount

        # Stable sort for readability
        journals.sort()
        branches.sort()

        # Header row: leave (0,0) blank, then branch names, then 'Total'
        sheet.set_column(0, 0, 30)
        sheet.write(0, 0, '', header)
        for c, branch in enumerate(branches, start=1):
            sheet.set_column(c, c, 22)
            sheet.write(0, c, branch, header)
        total_col = len(branches) + 1
        sheet.set_column(total_col, total_col, 22)
        sheet.write(0, total_col, 'Total', header)
        sheet.set_row(0, 28)

        # Body rows
        branch_totals = [0.0] * len(branches)
        grand_total   = 0.0
        for r, journal in enumerate(journals, start=1):
            sheet.write(r, 0, journal, header)
            row_total = 0.0
            for c, branch in enumerate(branches, start=1):
                amount = matrix.get((journal, branch), 0)
                sheet.write(r, c, amount, cell)
                row_total            += amount
                branch_totals[c - 1] += amount
            sheet.write(r, total_col, row_total, header)
            grand_total += row_total
            sheet.set_row(r, 22)

        # Footer Total row
        footer_row = len(journals) + 1
        sheet.write(footer_row, 0, 'Total', header)
        for c, btot in enumerate(branch_totals, start=1):
            sheet.write(footer_row, c, btot, header)
        sheet.write(footer_row, total_col, grand_total, header)
        sheet.set_row(footer_row, 24)

        return workbook

    def _generate_excel_type_payment(self, lines, workbook):
        sheet = workbook.add_worksheet('Payments Type')
        bold, cell, header = self._fmt(workbook)

        sheet.set_column(0, 1, 30)
        sheet.write('A1', 'Type',    header)
        sheet.write('B1', 'Total',   header)
        sheet.set_column(4, 6, 30)
        sheet.write('E1', 'Type',    header)
        sheet.write('F1', 'Payment', header)
        sheet.write('G1', 'Total',   header)

        norm = [(self._s(l[0]), self._s(l[1]), float(l[2] or 0), self._s(l[3]))
                for l in lines]

        row       = 1
        other_row = 1
        other_col = 4
        for src_type, grp1 in igrp(sorted(norm, key=lambda x: (x[3], x[0])),
                                    key=lambda x: x[3]):
            base_row  = row
            grp1_list = list(grp1)
            sheet.write(row,       0,         src_type, header)
            sheet.write(other_row, other_col, src_type, header)
            row       += 1
            other_row += 1
            total = 0
            for journal, grp2 in igrp(sorted(grp1_list, key=lambda x: x[0]),
                                       key=lambda x: x[0]):
                total_payment = 0
                sheet.write(other_row, other_col + 1, journal, header)
                for line in grp2:
                    if line[1] != 'None':
                        total         += line[2]
                        total_payment += line[2]
                sheet.write(other_row, other_col + 2, total_payment, header)
                other_row += 1
            sheet.write(base_row, 1, total, header)
        return workbook

    def _generate_excel_all(self, open_invoices, open_credit,
                            paid_invoices, paid_credits,
                            sro_orders, payments, workbook):
        """Summary sheet: tables placed side-by-side.

        Layout:
          TOP    left  cols 0-4: Open Invoices & Credits
                 right cols 6-8: Paid Invoices & Credits, then SRO
          BOTTOM left  cols 0-3: Payments by Journal & Branch
                 right cols 5-6: Payment Type Summary (type + total)
        """
        sheet = workbook.add_worksheet('All')
        bold, cell, header = self._fmt(workbook)

        title_fmt = workbook.add_format({
            'bold': 1, 'font_size': 13, 'align': 'center',
            'valign': 'vcenter', 'bg_color': '#2E4057',
            'font_color': '#FFFFFF', 'border': 1,
        })
        section_fmt = workbook.add_format({
            'bold': 1, 'font_size': 11, 'align': 'left',
            'valign': 'vcenter', 'bg_color': '#5B84B1',
            'font_color': '#FFFFFF', 'border': 1,
        })

        # Pre-set default column widths (expanded to 20 cols; the payment matrix
        # may add more — those are sized individually when the matrix is built)
        for c in range(20):
            sheet.set_column(c, c, 20)
        sheet.set_column(0, 0, 30)   # Journal column
        sheet.set_column(2, 2, 26)   # Customer column

        # Title (will be widened after we know total_pay_col; write it last)
        sheet.set_row(0, 28)

        L = 2
        R = 2

        # Left: Open Invoices & Credits
        sheet.merge_range(L, 0, L, 4, 'Open Invoices & Credits', section_fmt)
        sheet.set_row(L, 22); L += 1
        for c, lbl in enumerate(['Number', 'Branch', 'Customer', 'Total', 'Date']):
            sheet.write(L, c, lbl, header)
        sheet.set_row(L, 20); L += 1
        for line in list(open_invoices) + list(open_credit):
            sheet.write(L, 0, self._s(line[0]), cell)
            sheet.write(L, 1, self._s(line[1]), cell)
            sheet.write(L, 2, self._s(line[2]), cell)
            sheet.write(L, 3, float(line[3] or 0), cell)
            sheet.write(L, 4, self._s(line[4]), cell)
            sheet.set_row(L, 18); L += 1

        # Right: Paid Invoices & Credits
        sheet.merge_range(R, 6, R, 8, 'Paid Invoices & Credits', section_fmt)
        sheet.set_row(R, 22); R += 1
        sheet.write(R, 6, 'Branch', header)
        sheet.write(R, 7, 'Total',  header)
        sheet.set_row(R, 20); R += 1
        store = {}
        for line in paid_invoices:
            store[self._s(line[0])] = float(line[1] or 0)
        for line in paid_credits:
            k = self._s(line[0])
            store[k] = store.get(k, 0) + float(line[1] or 0)
        grand_paid = 0
        for branch, tot in store.items():
            sheet.write(R, 6, branch, cell)
            sheet.write(R, 7, tot,    cell)
            grand_paid += tot; R += 1
        sheet.write(R, 6, 'Grand Total', header)
        sheet.write(R, 7, grand_paid,    header); R += 1
        R += 1

        # Right: SRO Orders
        sheet.merge_range(R, 6, R, 8, 'SRO Orders', section_fmt)
        sheet.set_row(R, 22); R += 1
        sheet.write(R, 6, 'Branch', header)
        sheet.write(R, 7, 'Total',  header)
        sheet.set_row(R, 20); R += 1
        grand_sro = 0
        for line in sro_orders:
            sheet.write(R, 6, self._s(line[0]), cell)
            sheet.write(R, 7, float(line[1] or 0), cell)
            grand_sro += float(line[1] or 0); R += 1
        sheet.write(R, 6, 'Grand Total', header)
        sheet.write(R, 7, grand_sro,     header); R += 1

        next_row = max(L, R) + 2

        norm = [(self._s(l[0]), self._s(l[1]), float(l[2] or 0), self._s(l[3]))
                for l in payments]

        BL = next_row
        BR = next_row

        # --- pre-compute payment matrix axes so we know total width for title ---
        # Bottom-Left: Payments by Journal & Branch  (matrix layout like Branches Payments sheet)
        # Build unique journal/branch axes and (journal, branch) -> sum map
        pay_matrix   = {}
        pay_journals = []
        pay_branches = []
        j_seen2      = set()
        b_seen2      = set()
        for journal, branch, amount, _src in norm:
            if not journal or not branch:
                continue
            if journal not in j_seen2:
                j_seen2.add(journal)
                pay_journals.append(journal)
            if branch not in b_seen2:
                b_seen2.add(branch)
                pay_branches.append(branch)
            pay_matrix[(journal, branch)] = pay_matrix.get((journal, branch), 0) + amount
        pay_journals.sort()
        pay_branches.sort()

        total_pay_col = len(pay_branches) + 1          # column index of the "Total" column
        merge_end_col = total_pay_col                  # last column of this table

        # Now write the sheet title spanning the full width
        title_end = max(8, total_pay_col + 3)
        sheet.merge_range(0, 0, 0, title_end, 'Accounting Report - Full Summary', title_fmt)

        # Section title spanning all columns of the matrix
        if merge_end_col > 0:
            sheet.merge_range(BL, 0, BL, merge_end_col,
                              'Payments by Journal & Branch', section_fmt)
        else:
            sheet.write(BL, 0, 'Payments by Journal & Branch', section_fmt)
        sheet.set_row(BL, 22); BL += 1

        # Header row: blank corner, branch names, "Total"
        sheet.write(BL, 0, '', header)
        for c, branch in enumerate(pay_branches, start=1):
            # expand column width to fit if needed
            sheet.set_column(c, c, max(20, len(branch) + 4))
            sheet.write(BL, c, branch, header)
        sheet.write(BL, total_pay_col, 'Total', header)
        sheet.set_row(BL, 24); BL += 1

        # Body rows
        branch_pay_totals = [0.0] * len(pay_branches)
        grand_pay         = 0.0
        for journal in pay_journals:
            sheet.write(BL, 0, journal, header)
            row_total = 0.0
            for c, branch in enumerate(pay_branches, start=1):
                amount = pay_matrix.get((journal, branch), 0)
                sheet.write(BL, c, amount, cell)
                row_total                += amount
                branch_pay_totals[c - 1] += amount
            sheet.write(BL, total_pay_col, row_total, header)
            grand_pay += row_total
            sheet.set_row(BL, 20); BL += 1

        # Footer Total row
        sheet.write(BL, 0, 'Total', header)
        for c, btot in enumerate(branch_pay_totals, start=1):
            sheet.write(BL, c, btot, header)
        sheet.write(BL, total_pay_col, grand_pay, header)
        sheet.set_row(BL, 22); BL += 1

        # Bottom-Right: Payment Type Summary (grouped by type)
        # Place it two columns after the end of the payment matrix
        type_col = total_pay_col + 2
        sheet.set_column(type_col,     type_col,     20)
        sheet.set_column(type_col + 1, type_col + 1, 20)
        sheet.merge_range(BR, type_col, BR, type_col + 1, 'Payment Type Summary', section_fmt)
        sheet.set_row(BR, 22); BR += 1
        sheet.write(BR, type_col,     'Type',  header)
        sheet.write(BR, type_col + 1, 'Total', header)
        sheet.set_row(BR, 20); BR += 1
        grand_type = 0
        for src_type, grp1 in igrp(sorted(norm, key=lambda x: x[3]),
                                    key=lambda x: x[3]):
            tot = sum(l[2] for l in grp1)
            sheet.write(BR, type_col,     src_type, cell)
            sheet.write(BR, type_col + 1, tot,      cell)
            grand_type += tot
            sheet.set_row(BR, 18); BR += 1
        sheet.write(BR, type_col,     'Grand Total', header)
        sheet.write(BR, type_col + 1, grand_type,    header); BR += 1

        return workbook

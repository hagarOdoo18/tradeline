# -*- coding: utf-8 -*-
from odoo import fields, models, api, _
from odoo.exceptions import UserError
import xlsxwriter
from io import BytesIO
import base64
from datetime import datetime



class AccountInvoiceWizard(models.TransientModel):
    _name = 'account.invoice.duo.wizard'
    _description = 'Account Invoice Wizard'
    partner_id = fields.Many2one("res.partner", string="Customer")
    branch_id = fields.Many2one(
        comodel_name='res.branch',
        string='Branch',
        required=False)
    number = fields.Char(string='Invoice Number')
    journal_id = fields.Many2many('account.journal', string='Journal')
    date_from = fields.Date(string='Date From')
    date_to = fields.Date(string='Date To')
    excel_file = fields.Binary(readonly=True)
    file_name = fields.Char(readonly=True)


    def action_account_invoice_search(self):
        invoices = self._search_invoices()
        if not invoices:
            raise UserError(_('No invoices found for the selected criteria.'))
        return self.generate_excel(invoices)

    def _search_invoices(self):
        domain = [ ('move_type', 'in', ('out_invoice', 'out_refund'))]
        if self.date_from:
            domain.append(('invoice_date', '>=', self.date_from))
        if self.date_to:
            domain.append(('invoice_date', '<=', self.date_to))
        if self.partner_id:
            domain.append(('partner_id', '=', self.partner_id.id))
        if self.number:
            domain.append(('name', '=', self.number))
        if self.branch_id:
            domain.append(('branch_id', '=', self.branch_id.id))
        invoices = self.env['account.move'].search(domain)
        if self.journal_id:
            invoices = self.get_invoices_by_journal(self.journal_id,self.date_from,self.date_to)
        return invoices



    @api.model
    def get_invoices_by_journal(self, journal_id, date_from=None, date_to=None):
        """Find all invoices linked to payments made through specific journals.
        Uses bulk SQL instead of per-payment ORM loops.
        """
        cr = self.env.cr
        jids = tuple(journal_id.ids)
        if not jids:
            return self.env['account.move']

        params = {
            'jids':  jids,
            'df':    str(date_from) if date_from else None,
            'dt':    str(date_to)   if date_to   else None,
        }

        # ── reconciled payments via account_partial_reconcile ──────────────
        cr.execute("""
            SELECT DISTINCT inv.id
            FROM account_payment ap
            JOIN account_move_line aml_pay
                ON  aml_pay.move_id = ap.move_id
            JOIN account_account aa_pay
                ON  aa_pay.id          = aml_pay.account_id
                AND aa_pay.account_type = 'asset_receivable'
            JOIN account_partial_reconcile apr
                ON  apr.credit_move_id = aml_pay.id
            JOIN account_move_line aml_inv
                ON  aml_inv.id = apr.debit_move_id
            JOIN account_move inv
                ON  inv.id        = aml_inv.move_id
                AND inv.move_type IN ('out_invoice', 'out_refund')
                AND inv.state     = 'posted'
            WHERE ap.journal_id = ANY(%(jids)s)
              AND (%(df)s IS NULL OR ap.date >= %(df)s::date)
              AND (%(dt)s IS NULL OR ap.date <= %(dt)s::date)
        """, params)
        inv_ids = {r[0] for r in cr.fetchall()}

        # ── POS payments ───────────────────────────────────────────────────
        try:
            cr.execute("""
                SELECT DISTINCT inv.id
                FROM pos_payment pp
                JOIN pos_payment_method ppm ON ppm.id = pp.payment_method_id
                JOIN pos_order po  ON po.id  = pp.pos_order_id
                JOIN account_move inv ON inv.id = po.account_move
                WHERE ppm.journal_id = ANY(%(jids)s)
                  AND (%(df)s IS NULL OR pp.payment_date::date >= %(df)s::date)
                  AND (%(dt)s IS NULL OR pp.payment_date::date <= %(dt)s::date)
            """, params)
            inv_ids |= {r[0] for r in cr.fetchall()}
        except Exception:
            cr.rollback()

        if not inv_ids:
            return self.env['account.move']
        return self.env['account.move'].browse(list(inv_ids))

    def generate_excel(self, invoices):
        filename = 'Account Invoices'
        output = BytesIO()

        workbook = xlsxwriter.Workbook(output, {'in_memory': True})
        sheet = workbook.add_worksheet('Account Invoices Report')

        header_format = workbook.add_format({
            'bold': True, 'border': 1, 'bg_color': '#AAB7B8',
            'align': 'center', 'valign': 'vcenter',
            'text_wrap': True, 'font_size': 10
        })

        cell_format = workbook.add_format({
            'font_name': 'KacstBook', 'font_size': 10,
            'align': 'center', 'valign': 'vcenter',
            'text_wrap': True, 'border': 1
        })

        headers = [
            'No', 'Date', 'Invoice Number', 'Branch', 'Customer Name', 'Phone',
            'Payment', 'Payment Amount', 'Ref', 'Tax Excluded', 'Tax14',
            'Total', 'Tax1','Tax2', 'Tax3', 'Tax5', 'Total Net', 'Amount Due'
        ]
        sheet.set_column(1, 50, 20)
        for col, header in enumerate(headers):
            sheet.write(0, col, header, header_format)



        def write_row(row, idx, inv, journal, payment_amount, show_residual):

            sign = 1 if inv.move_type == 'out_invoice' else -1
            amount_total = inv.amount_untaxed_in_currency_signed +inv.tax_t1  * sign

            values = [
                idx,
                str(inv.invoice_date or ''),
                inv.name or 'None',
                inv.branch_id.name or 'None',
                inv.partner_id.name or 'None',
                inv.partner_id.phone or 'None',
                journal or 'None',
                payment_amount if payment_amount < 0 else payment_amount *sign ,
                inv.invoice_origin or inv.ref,
                inv.amount_untaxed_in_currency_signed  if show_residual else 0,
                round( inv.tax_t1 * sign ,2) if show_residual else 0,
                round(amount_total,2)  if show_residual else 0,
                round(inv.tax_t2 * sign,2)  if show_residual else 0,
                round(inv.tax_t2_t * sign,2)  if show_residual else 0,
                round(inv.tax_t3 * sign,2)  if show_residual else 0,
                round( inv.tax_t5 * sign,2)  if show_residual else 0,
                round(inv.amount_total_in_currency_signed,2)  if show_residual else 0,
                round(inv.amount_residual_signed ,2)  if show_residual else 0,
            ]

            for col, val in enumerate(values):
                sheet.write(row, col, val, cell_format)

        # ── Bulk-fetch all payments for all invoices in two SQL queries ──────
        cr  = self.env.cr
        inv_ids = invoices.ids
        pay_map = {}   # {inv_id: [(journal_name, amount), ...]}

        if inv_ids:
            # reconciled account.payment
            cr.execute("""
                SELECT aml_inv.move_id, aj.name, apr.amount
                FROM account_move_line aml_inv
                JOIN account_partial_reconcile apr
                    ON apr.debit_move_id = aml_inv.id
                JOIN account_move_line aml_pay
                    ON aml_pay.id = apr.credit_move_id
                JOIN account_payment ap
                    ON ap.move_id = aml_pay.move_id
                JOIN account_journal aj
                    ON aj.id = ap.journal_id
                WHERE aml_inv.move_id = ANY(%s)
            JOIN account_account aa_inv
                    ON  aa_inv.id          = aml_inv.account_id
                   AND aa_inv.account_type = 'asset_receivable'
            """, (inv_ids,))
            for inv_id, jname, amount in cr.fetchall():
                pay_map.setdefault(inv_id, []).append((jname, float(amount or 0)))

            # POS payments
            try:
                cr.execute("""
                    SELECT inv.id, aj.name, pp.amount
                    FROM account_move inv
                    JOIN pos_order po  ON po.account_move  = inv.id
                    JOIN pos_payment pp ON pp.pos_order_id = po.id
                    JOIN pos_payment_method ppm ON ppm.id  = pp.payment_method_id
                    JOIN account_journal aj ON aj.id = ppm.journal_id
                    WHERE inv.id = ANY(%s)
                      AND inv.id NOT IN %(done)s
                """, (inv_ids, {'done': tuple(pay_map.keys()) or (0,)}))
                for inv_id, jname, amount in cr.fetchall():
                    if inv_id not in pay_map:
                        pay_map.setdefault(inv_id, []).append((jname, float(amount or 0)))
            except Exception:
                cr.rollback()

        row = 1
        printed_invoices = set()

        for idx, inv in enumerate(invoices, start=1):
            payments = pay_map.get(inv.id, [])

            if payments:
                for jname, amount in payments:
                    write_row(
                        row=row,
                        idx=idx,
                        inv=inv,
                        journal=jname,
                        payment_amount=amount,
                        show_residual=inv.id not in printed_invoices
                    )
                    printed_invoices.add(inv.id)
                    row += 1
            else:
                write_row(
                    row=row,
                    idx=idx,
                    inv=inv,
                    journal='',
                    payment_amount=0,
                    show_residual=inv.id not in printed_invoices
                )
                printed_invoices.add(inv.id)
                row += 1

        workbook.close()
        output.seek(0)

        self.file_name = f'invoices_{datetime.today().date()}.xlsx'
        self.excel_file = base64.b64encode(output.read())

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.invoice.duo.wizard',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }
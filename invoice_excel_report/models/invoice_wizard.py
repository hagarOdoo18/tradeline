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
        """
        Utility method: find all invoices linked to payments made through a specific journal.

        Usage:
            invoices = env['invoice.payment.search.wizard'].get_invoices_by_journal(5)
            invoices = env['invoice.payment.search.wizard'].get_invoices_by_journal(
                journal_id=5,
                date_from='2024-01-01',
                date_to='2024-12-31'
            )
        """

        domain = [('journal_id', 'in', journal_id.ids)]
        if date_from:
            domain += [('date', '>=', date_from)]
        if date_to:
            domain += [('date', '<=', date_to)]

        payments = self.env['account.payment'].search(domain)
        pos_payments = self.env['pos.payment'].search(([('payment_method_id.journal_id','in',journal_id.ids),('payment_date', '>=', date_from),('payment_date', '<=', date_to)]))
        invoices = self.env['account.move']
        for payment in payments:
            invoices |= self._get_linked_invoices_from_payment(payment)
        pos_orders = pos_payments.mapped('pos_order_id')
        invoices |= pos_orders.mapped('account_move').filtered(lambda m: m.exists())

        return invoices

    def _get_linked_invoices_from_payment(self, payment):
        """
        Return all invoices (account.move) reconciled with the given payment.

        In Odoo 18, a payment creates journal items (account.move.line).
        Invoices are linked via the reconciliation of those journal items
        with the invoice's receivable/payable lines.
        """
        invoices = self.env['account.move']

        # Get all move lines of the payment
        payment_lines = payment.move_id.line_ids.filtered(
            lambda l: l.account_id.account_type in (
                'asset_receivable', 'liability_payable'
            )
        )

        # Find all matched move lines (reconciled counterparts)
        for line in payment_lines:
            # matched_debit_ids / matched_credit_ids hold reconciliation records
            reconciled_lines = (
                    line.matched_debit_ids.mapped('debit_move_id') +
                    line.matched_credit_ids.mapped('credit_move_id')
            )
            for rec_line in reconciled_lines:
                if line.move_id.move_type in ('out_invoice', 'out_refund'):
                    if rec_line.move_id != payment.move_id:
                        invoices |= rec_line.move_id


        return invoices

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

        row = 1
        printed_invoices = set()

        for idx, inv in enumerate(invoices, start=1):
            payments = inv._get_reconciled_payments()

            if payments:
                for payment in payments:
                    write_row(
                        row=row,
                        idx=idx,
                        inv=inv,
                        journal=payment.journal_id.name,
                        payment_amount=payment.amount,
                        show_residual=inv.id not in printed_invoices
                    )
                    printed_invoices.add(inv.id)
                    row += 1
            elif inv.pos_order_ids.payment_ids:
                for payment in inv.pos_order_ids.payment_ids:
                    write_row(
                        row=row,
                        idx=idx,
                        inv=inv,
                        journal=payment.payment_method_id.journal_id.name,
                        payment_amount=payment.amount,
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
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
            invoices = invoices.filtered(lambda inv: inv.journal_id.id in self.journal_id.ids)
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
            'Total', 'Tax1', 'Tax3', 'Tax5', 'Total Net', 'Amount Due'
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
                inv.invoice_origin or 'None',
                inv.amount_untaxed_in_currency_signed  if show_residual else 0,
                inv.tax_t1 * sign  if show_residual else 0,
                amount_total  if show_residual else 0,
                inv.tax_t2 * sign  if show_residual else 0,
                inv.tax_t3 * sign  if show_residual else 0,
                inv.tax_t5 * sign  if show_residual else 0,
                 inv.amount_total_in_currency_signed  if show_residual else 0,
                inv.amount_residual_signed   if show_residual else 0,
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
            else:
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
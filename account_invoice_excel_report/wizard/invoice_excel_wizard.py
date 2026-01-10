
# -*- coding: utf-8 -*-
from odoo import fields, models, api, _
from odoo.exceptions import UserError
import xlsxwriter
from io import BytesIO
import base64
from datetime import datetime

class AccountInvoiceWizard(models.TransientModel):
    _name = 'account.invoice.wizard2'
    _description = 'Account Invoice Excel Wizard'

    date_from = fields.Date(string='Date From', required=True)
    date_to = fields.Date(string='Date To', required=True)

    def action_export_excel(self):
        self.ensure_one()

        invoices = self.env['account.move'].search([
            ('invoice_date', '>=', self.date_from),
            ('invoice_date', '<=', self.date_to),
            ('state', '=', 'posted'),
            ('move_type', 'in', ('out_invoice', 'out_refund')),
        ])

        order_payments = self.env['account.payment'].with_context(
            allowed_company_ids=self.env.companies.ids
        ).search([
            ('sale_order_id', '!=', False),

            ('date', '>=', self.date_from),
            ('date', '<=', self.date_to),
        ])

        if not invoices:
            raise UserError(_('No invoices found for this period'))

        report = self.generate_excel(invoices,order_payments)

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'report.excel',
            'res_id': report.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def generate_excel(self, invoices,order_payments):
        filename = f'Payment And Branches {datetime.today().strftime("%Y-%m-%d")}.xlsx'

        output = BytesIO()
        workbook = xlsxwriter.Workbook(output, {'in_memory': True})
        sheet = workbook.add_worksheet('Invoices Report')

        header_format = workbook.add_format({
            'bold': True,
            'border': 1,
            'align': 'center',
            'valign': 'vcenter',
            'bg_color': '#AAB7B8',
        })

        cell_format = workbook.add_format({
            'border': 1,
            'align': 'center',
        })

        sheet.set_column(0, 0, 30)
        sheet.set_column(1, 50, 20)

        master_dic = {}
        branch_set = set()

        for move in invoices:
            branch = move.branch_id.name or _('No Branch')
            payments = move._get_reconciled_payments()
            if payments:
                for payment in payments:
                    journal = payment.journal_id.name
                    if move.move_type == 'out_invoice':
                        amount = payment.amount
                    else:
                        amount = payment.amount * -1
                    branch_set.add(branch)
                    if journal not in master_dic.keys():
                        master_dic.setdefault(journal, {})
                    if branch not in master_dic[journal].keys():
                        master_dic[journal].setdefault(branch, 0.0)
                    master_dic[journal][branch] += amount
            else:

                for payment in move.pos_order_ids.payment_ids:
                    journal=payment.payment_method_id.journal_id.name
                    amount = payment.amount
                    branch_set.add(branch)
                    if branch =='Water Way 3 TLS':
                        print('Water Way 3 TLS')
                    if journal not in master_dic.keys():
                        master_dic.setdefault(journal, {})
                    if branch not in master_dic[journal].keys():
                        master_dic[journal].setdefault(branch, 0.0)
                    master_dic[journal][branch] += amount



        for payment in order_payments:
            branch_set.add(payment.branch_id.name)
            amount=payment.amount
            journal = payment.journal_id.name
            if journal not in master_dic.keys():
                master_dic.setdefault(journal, {})
            if payment.branch_id.name not in master_dic[journal].keys():
                master_dic[journal].setdefault(payment.branch_id.name, 0.0)
            master_dic[journal][payment.branch_id.name] += amount

        branches = list(branch_set)

        # Header
        sheet.write(0, 0, _('Payments'), header_format)
        for col, branch in enumerate(branches, start=1):
            sheet.write(0, col, branch, header_format)

        # Data
        row = 1
        for journal, values in master_dic.items():
            sheet.write(row, 0, journal, cell_format)
            for col, branch in enumerate(branches, start=1):
                sheet.write(row, col, values.get(branch, 0.0), cell_format)
            row += 1

        workbook.close()
        output.seek(0)

        report = self.env['report.excel'].create({
            'file_name': filename,
            'excel_file': base64.b64encode(output.read()),
        })

        return report

    class AccountInvoiceWizard(models.TransientModel):
        _name = 'account.invoice.wizard'
        _description = 'Account Invoice Excel Wizard'

        partner_id = fields.Many2one('res.partner')
        branch_id = fields.Many2one('res.branch')
        journal_ids = fields.Many2many('account.journal')
        invoice_number = fields.Char()
        date_from = fields.Date()
        date_to = fields.Date()

        excel_file = fields.Binary(readonly=True)
        file_name = fields.Char(readonly=True)

        def action_export_excel(self):
            domain = [
                ('move_type', 'in', ('out_invoice', 'out_refund')),
                ('state', '=', 'posted')
            ]

            if self.date_from:
                domain.append(('invoice_date', '>=', self.date_from))
            if self.date_to:
                domain.append(('invoice_date', '<=', self.date_to))
            if self.partner_id:
                domain.append(('partner_id', '=', self.partner_id.id))
            if self.branch_id:
                domain.append(('branch_id', '=', self.branch_id.id))
            if self.invoice_number:
                domain.append(('name', '=', self.invoice_number))
            if self.journal_ids:
                domain.append(('journal_id', 'in', self.journal_ids.ids))

            invoices = self.env['account.move'].search(domain)

            if not invoices:
                raise UserError(_('No invoices found'))

            return self._generate_excel(invoices)

        def _generate_excel(self, invoices):
            output = BytesIO()
            workbook = xlsxwriter.Workbook(output, {'in_memory': True})
            sheet = workbook.add_worksheet('Invoices')

            header = workbook.add_format({'bold': True, 'border': 1,'bg_color': '#AAB7B8','align': 'center', 'valign': 'vcenter',})
            cell = workbook.add_format( {'font_name': 'KacstBook', 'font_size': 10, 'align': 'center', 'valign': 'vcenter', 'text_wrap': True, 'border': 1})

            headers = ['No', 'Date', 'Invoice','Branch', 'Customer','Mobile', 'Journal', 'Journal Amount','Ref','Tax Excluded','Tax14','Total','Tax1', 'Tax3','Tax5','Total Net', 'Amount Due']
            for col, h in enumerate(headers):
                sheet.write(0, col, h, header)

            row = 1
            for i, move in enumerate(invoices, start=1):
                for line in move.matched_payment_ids.filtered(lambda l: l.state == 'paid'):
                    if move.move_type == 'out_invoice':
                        amount = line.amount
                    else:
                        amount = line.amount * -1
                    sheet.write(row, 0, i, cell)
                    sheet.write(row, 1, str(move.invoice_date or ''), cell)
                    sheet.write(row, 2, move.name or '', cell)
                    sheet.write(row, 3, move.branch_id.name or '', cell)
                    sheet.write(row, 4, move.partner_id.name or '', cell)
                    sheet.write(row, 5, move.partner_id.mobile or '', cell)
                    sheet.write(row, 6, line.journal_id.name or '', cell)
                    sheet.write(row, 7, amount or '', cell)
                    sheet.write(row, 8, move.invoice_origin, cell)
                    sheet.write(row, 9, move.amount_untaxed_signed, cell)
                    sheet.write(row, 10, move.tax_t1  if move.move_type == 'out_invoice' else  move.tax_t1*-1, cell)
                    sheet.write(row, 11, move.total if move.move_type == 'out_invoice' else  move.total*-1, cell)
                    sheet.write(row, 12, move.tax_t2 if move.move_type == 'out_invoice' else  move.tax_t2*-1, cell)
                    sheet.write(row, 13, move.tax_t3 if move.move_type == 'out_invoice' else  move.tax_t3*-1, cell)
                    sheet.write(row, 14, move.tax_t5 if move.move_type == 'out_invoice' else  move.tax_t5*-1, cell)
                    sheet.write(row, 15, move.amount_total_signed, cell)
                    sheet.write(row, 16, move.amount_residual_signed, cell)
                    row += 1

            workbook.close()
            output.seek(0)

            self.file_name = f'invoices_{datetime.today().date()}.xlsx'
            self.excel_file = base64.b64encode(output.read())

            return {
                'type': 'ir.actions.act_window',
                'res_model': 'account.invoice.wizard',
                'res_id': self.id,
                'view_mode': 'form',
                'target': 'new',
            }


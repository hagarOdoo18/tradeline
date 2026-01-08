from odoo import models, fields
import xlsxwriter
from io import BytesIO
import base64
from datetime import datetime
class AccountBranchReportWizard(models.TransientModel):
    _name = 'account.branch.report.wizard'
    _description = 'Branch Account Excel Report Wizard'

    branches_ids = fields.Many2many(
        'res.branch',
        string='Branches',
        domain=lambda self: [('company_id', 'in', self.env.companies.ids)],
        required=True
    )
    date_from = fields.Date(required=True)
    date_to = fields.Date(required=True)

    excel_file = fields.Binary(readonly=True)
    file_name = fields.Char(readonly=True)

    def action_export_excel(self):
      
      
            output = BytesIO()
            workbook = xlsxwriter.Workbook(output, {'in_memory': True})
            header = workbook.add_format({'bold': True, 'border': 1, 'align': 'center'})
            text = workbook.add_format({'border': 1})
            amount = workbook.add_format({'border': 1, 'num_format': '#,##0.00'})

            summary = workbook.add_worksheet('Summary')
            summary_headers = ['Branch', 'Invoices , Credits And Sales Order Count',
                               'Total Invoices , Credits And Sales Order', 'Total Payments', 'Balance']
            for col, h in enumerate(summary_headers):
                summary.write(0, col, h, header)
            summary_row = 1

            headers = ['Branch', 'Invoice Or Credit', 'Sale Order', 'Customer', 'Payment', 'Amount']


            for branch in self.branches_ids:
                sheet = workbook.add_worksheet(branch.name[:31])
                sheet.set_column(0, 5, 30)
                for col, h in enumerate(headers):
                    sheet.write(0, col, h, header)

                row = 1
                invoices = self.env['account.move'].with_context(
                    allowed_company_ids=self.env.companies.ids
                ).search([
                    ('move_type', '=', 'out_invoice'),
                    ('state', '=', 'posted'),
                    ('branch_id', '=', branch.id),
                    ('company_id', '=', branch.company_id.id),
                    ('invoice_date', '>=', self.date_from),
                    ('invoice_date', '<=', self.date_to),
                ])
                order_payments = self.env['account.payment'].with_context(
                    allowed_company_ids=self.env.companies.ids
                ).search([
                    ('sale_order_id', '!=', False),
                    ('branch_id', '=', branch.id),
                    ('company_id', '=', branch.company_id.id),
                    ('date', '>=', self.date_from),
                    ('date', '<=', self.date_to),
                ])

                credits = self.env['account.move'].with_context(
                    allowed_company_ids=self.env.companies.ids
                ).search([
                    ('move_type', '=', 'out_refund'),
                    ('state', '=', 'posted'),
                    ('branch_id', '=', branch.id),
                    ('company_id', '=', branch.company_id.id),
                    ('invoice_date', '>=', self.date_from),
                    ('invoice_date', '<=', self.date_to),
                ])

                total_invoice = total_payment = 0.0

                for inv in invoices:
                    total_invoice += inv.amount_total
                    payments = inv._get_reconciled_payments()
                    if payments:
                        for payment in payments:
                            sheet.write_row(row, 0,
                                            [branch.name, inv.name, '', inv.partner_id.name, payment.journal_id.name,
                                             payment.amount],
                                            text)
                            sheet.write(row, 5, inv.amount_total, amount)
                            total_payment += payment.amount
                            row += 1
                    else:
                        for payment in inv.pos_order_ids.payment_ids:
                            sheet.write_row(row, 0,
                                            [branch.name, inv.name, '', inv.partner_id.name, payment.payment_method_id.name,
                                             payment.amount],
                                            text)
                            sheet.write(row, 5, inv.amount_total, amount)
                            total_payment += payment.amount
                            row += 1

                for inv in credits:
                    total_invoice -= inv.amount_total

                    payments = inv._get_reconciled_payments()
                    if payments:
                        for payment in payments:
                            sheet.write_row(row, 0,
                                            [branch.name, inv.name, '', inv.partner_id.name, payment.journal_id.name,
                                             -1 * payment.amount],
                                            text)
                            sheet.write(row, 5, -1 * inv.amount_total, amount)
                            total_payment -= payment.amount
                            row += 1
                    else:
                        for payment in inv.pos_order_ids.payment_ids:
                            sheet.write_row(row, 0,
                                            [branch.name, inv.name, '', inv.partner_id.name, payment.payment_method_id.name,
                                             -1 * payment.amount],
                                            text)
                            sheet.write(row, 5, -1 * inv.amount_total, amount)
                            total_payment -= payment.amount

                            row += 1

                for payment in order_payments:
                    total_invoice += payment.amount
                    sheet.write_row(row, 0,
                                    [branch.name, '', payment.sale_order_id.name, payment.sale_order_id.partner_id.name,
                                     payment.journal_id.name,
                                     payment.amount],
                                    text)
                    sheet.write(row, 5, payment.amount, amount)
                    row += 1
                summary.set_column(0, 5, 30)
                summary.write_row(summary_row, 0, [
                    branch.name,
                    len(invoices) + len(credits) + len(order_payments),
                    total_invoice,
                    total_payment,
                    total_invoice - total_payment
                ], amount)
                summary_row += 1
            workbook.close()
            output.seek(0)

            self.file_name = f'invoices_{datetime.today().date()}.xlsx'
            self.excel_file = base64.b64encode(output.read())

            return {
                'type': 'ir.actions.act_window',
                'res_model': 'account.branch.report.wizard',
                'res_id': self.id,
                'view_mode': 'form',
                'target': 'new',
            }


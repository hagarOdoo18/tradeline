# -*- coding: utf-8 -*-
from odoo import fields, models, api, _
from odoo.exceptions import UserError
import xlsxwriter
from io import BytesIO
import base64
from datetime import datetime


class ReportExcel(models.TransientModel):
    _name = 'report.excel'
    _description = 'Excel Download'

    excel_file = fields.Binary('Download Excel', attachment=True, readonly=True)
    file_name = fields.Char('Excel File', size=64)


class AccountInvoiceReportWizard(models.TransientModel):
    _name = 'account.invoice.report.tax.wizard'
    _description = 'Tax Invoice Excel Report Wizard'

    excel_file = fields.Binary('Download Excel', attachment=True, readonly=True)
    file_name = fields.Char('Excel File', size=64)

    date_from = fields.Date(string='Date from', required=True)
    date_to = fields.Date(string='Date to', required=True)

    _sql_constraints = [
        ('check_dates', 'CHECK(date_from <= date_to)', "End date must be greater than or equal to start date")
    ]

    def action_invoice_search(self):
        """ Search invoices in Odoo 18 """

        invoices = self.env['account.move'].search([
            ('invoice_date', '>=', self.date_from),
            ('invoice_date', '<=', self.date_to),
            ('state', '=', 'posted'),
            ('move_type', 'in', ['out_invoice','out_refund']),   # sales invoices
        ], order='invoice_date')

        act = self.generate_excel(invoices)

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'report.excel',
            'res_id': act.id,
            'view_type': 'form',
            'view_mode': 'form',
            'target': 'new',
        }

    def generate_excel(self, invoices):
        filename = 'Invoices_'

        output = BytesIO()
        workbook = xlsxwriter.Workbook(output, {'in_memory': True})
        sheet = workbook.add_worksheet('Invoices')

        header_format = workbook.add_format({
            'bold': True,
            'border': 1,
            'align': 'center',
            'valign': 'vcenter',
            'bg_color': '#AAB7B8',
            'font_size': 10,
            'text_wrap': True,
        })

        line_format = workbook.add_format({
            'border': 1,
            'align': 'center',
            'valign': 'vcenter',
            'font_size': 10,
            'text_wrap': True,
        })

        # Set columns
        sheet.set_column(0, 15, 20)

        headers = [
            'Invoice Date', 'Invoice No.', 'NUM.', 'Customer Name', 'Mobile',
            'Tax ID', 'National ID', 'Passport No.', 'Untaxed', 'Tax14', 'Total',
            'Tax1', 'Tax3','Tax5', 'Total Net', 'Net Converted', 'Currency'
        ]

        for col, head in enumerate(headers):
            sheet.write(0, col, head, header_format)

        row = 1

        for inv in invoices:

            number_split = inv.name.split('/') if inv.name else ['']

            # currency conversion
            currency_rate = self.env['res.currency']._get_conversion_rate(
                inv.company_currency_id,
                inv.currency_id,
                inv.company_id,
                inv.invoice_date
            )

            total_converted = inv.amount_total_signed * (1 / currency_rate) if currency_rate else 0

            # custom tax fields — keep if exists
            tax_t1 = getattr(inv, 'tax_t1', 0)
            tax_t2 = getattr(inv, 'tax_t2', 0)
            tax_t3 = getattr(inv, 'tax_t3', 0)
            tax_t5 = getattr(inv, 'tax_t5', 0)
            total = getattr(inv, 'total', inv.amount_total_signed)

            sheet.write(row, 0, inv.invoice_date.strftime('%d.%m.%Y') if inv.invoice_date else '', line_format)
            sheet.write(row, 1, inv.name or '', line_format)
            sheet.write(row, 2, number_split[-1], line_format)
            sheet.write(row, 3, inv.partner_id.name or '', line_format)
            sheet.write(row, 4, inv.partner_id.mobile or '', line_format)
            sheet.write(row, 5, inv.partner_id.vat or '', line_format)
            sheet.write(row, 6, getattr(inv.partner_id, 'national_id', ''), line_format)
            sheet.write(row, 7, getattr(inv.partner_id, 'passport_no', ''), line_format)
            sheet.write(row, 8, inv.amount_untaxed_signed, line_format)
            sheet.write(row, 9, total, line_format)
            sheet.write(row, 10, tax_t1, line_format)
            sheet.write(row, 11, tax_t2, line_format)
            sheet.write(row, 12, tax_t3, line_format)
            sheet.write(row, 13, tax_t5, line_format)
            sheet.write(row, 14, inv.amount_total_signed, line_format)
            sheet.write(row, 15, total_converted, line_format)
            sheet.write(row, 16, inv.currency_id.name, line_format)

            row += 1

        workbook.close()
        output.seek(0)

        self.write({
            'file_name': filename + datetime.today().strftime('%Y-%m-%d') + '.xlsx',
            'excel_file': base64.b64encode(output.read())
        })

        act_id = self.env['report.excel'].create({
            'file_name': self.file_name,
            'excel_file': self.excel_file
        })

        return act_id

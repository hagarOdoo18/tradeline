# -*- coding: utf-8 -*-
from odoo import fields, models, api, _
from odoo.exceptions import UserError
import xlsxwriter
from io import BytesIO
import base64
from datetime import datetime

from sympy.physics.units import amount


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

        return act

    @staticmethod
    def _invoice_amounts(inv):
        sign = 1 if inv.move_type == 'out_invoice' else -1
        currency_round = inv.currency_id.round
        tax_t1 = currency_round(sign * (inv.tax_t1 or 0.0))
        return {
            'tax_t1': tax_t1,
            'tax_t2': currency_round(sign * (inv.tax_t2 or 0.0)),
            'tax_t2_t': currency_round(sign * (inv.tax_t2_t or 0.0)),
            'tax_t3': currency_round(sign * (inv.tax_t3 or 0.0)),
            'tax_t5': currency_round(sign * (inv.tax_t5 or 0.0)),
            'total': currency_round(inv.amount_untaxed_in_currency_signed + tax_t1),
            'total_net': currency_round(inv.amount_total_in_currency_signed),
            'total_converted': inv.company_currency_id.round(inv.amount_total_signed),
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
        sheet.set_column(0, 16, 20)

        headers = [
            'Invoice Date', 'Invoice No.', 'NUM.', 'Customer Name', 'Mobile',
            'Tax ID ','National ID',  'Passport No.', 'Untaxed', 'Tax14', 'Total',
            'Tax1', 'Tax2','Tax3','Tax5', 'Total Net', 'Net Converted', 'Currency'
        ]

        for col, head in enumerate(headers):
            sheet.write(0, col, head, header_format)

        row = 1
        for inv in invoices:
            partner = inv.partner_id

            number = inv.name.split('/')[-1] if inv.name else ''

            # ===== Taxes =====
            amounts = self._invoice_amounts(inv)


            # ===== Partner VAT logic =====
            local_vat = partner.vat if partner.mobile_type == 'local' else ''
            foreign_vat = partner.vat if partner.mobile_type != 'local' else ''

            # ===== Excel write (single call) =====
            sheet.write_row(row, 0, [
                inv.invoice_date.strftime('%d.%m.%Y') if inv.invoice_date else '',
                inv.name or '',
                number,
                partner.name or '',
                partner.mobile or '',
                local_vat if partner.company_type =='company' else'',
                local_vat if partner.company_type !='company' else'',
                foreign_vat,
                inv.amount_untaxed_in_currency_signed,
                amounts['tax_t1'],
                amounts['total'],
                amounts['tax_t2'],
                amounts['tax_t2_t'],
                amounts['tax_t3'],
                amounts['tax_t5'],
                amounts['total_net'],
                amounts['total_converted'],
                inv.currency_id.name,
            ], line_format)

            row += 1

        workbook.close()
        output.seek(0)

        self.file_name = f'invoices_{datetime.today().date()}.xlsx'
        self.excel_file = base64.b64encode(output.read())

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.invoice.report.tax.wizard',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }


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

        if not invoices:
            raise UserError(_('No invoices found for this period'))

        report = self.generate_excel(invoices)

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'report.excel',
            'res_id': report.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def generate_excel(self, invoices):
        filename = f'Account_Invoices_{datetime.today().strftime("%Y-%m-%d")}.xlsx'

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
        sheet.set_column(1, 20, 20)

        master_dic = {}
        branch_set = set()

        for move in invoices:
            branch = move.bracnh_id.name or _('No Branch')

            for line in move.matched_payment_ids.filtered(lambda l: l.state == 'paid'):
                journal = line.journal_id.name
                if move.move_type == 'out_invoice':
                    amount = line.ammount
                else:
                    amount = line.ammount *-1


                branch_set.add(branch)

                master_dic.setdefault(journal, {})
                master_dic[journal].setdefault(branch, 0.0)
                master_dic[journal][branch] += amount

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


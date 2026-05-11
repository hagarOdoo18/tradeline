# -*- coding: utf-8 -*-
from odoo import fields, models
import xlsxwriter
from io import BytesIO
import base64
from datetime import datetime


class AccountBranchReportWizard(models.TransientModel):
    _name        = 'account.branch.report.wizard'
    _description = 'Branch Account Excel Report Wizard'

    branches_ids = fields.Many2many(
        'res.branch',
        string='Branches',
        domain=lambda self: [('company_id', 'in', self.env.companies.ids)],
        required=True,
    )
    date_from = fields.Date(required=True)
    date_to   = fields.Date(required=True)

    excel_file = fields.Binary(readonly=True)
    file_name  = fields.Char(readonly=True)

    # ------------------------------------------------------------------
    # shared data fetch  (used by both Excel export and list view)
    # ------------------------------------------------------------------

    def _get_branch_data(self, branch):
        """Return (invoices, credits, order_payments) for one branch."""
        ctx = {'allowed_company_ids': self.env.companies.ids}
        base = [
            ('branch_id',  '=', branch.id),
            ('company_id', '=', branch.company_id.id),
        ]
        invoices = self.env['account.move'].with_context(**ctx).search(base + [
            ('move_type',    '=',  'out_invoice'),
            ('state',        '=',  'posted'),
            ('invoice_date', '>=', self.date_from),
            ('invoice_date', '<=', self.date_to),
        ])
        credits = self.env['account.move'].with_context(**ctx).search(base + [
            ('move_type',    '=',  'out_refund'),
            ('state',        '=',  'posted'),
            ('invoice_date', '>=', self.date_from),
            ('invoice_date', '<=', self.date_to),
        ])
        order_payments = self.env['account.payment'].with_context(**ctx).search(base + [
            ('sale_order_id', '!=', False),
            ('state',         '=',  'paid'),
            ('date',          '>=', self.date_from),
            ('date',          '<=', self.date_to),
        ])
        return invoices, credits, order_payments

    # ------------------------------------------------------------------
    # View Payment Report  (creates transient lines then opens list)
    # ------------------------------------------------------------------

    def action_view_payment_report(self):
        self.ensure_one()

        # clear any previous lines for this wizard record
        self.env['payment.report'].search(
            [('wizard_id', '=', self.id)]
        ).unlink()

        lines = []

        for branch in self.branches_ids:
            invoices, credits, order_payments = self._get_branch_data(branch)

            # ---- invoices ------------------------------------------------
            for inv in invoices:
                payments = inv._get_reconciled_payments()
                if payments:
                    for pmt in payments:
                        lines.append({
                            'wizard_id':    self.id,
                            'branch_name':  branch.name,
                            'date':         pmt.date,
                            'invoice_name': inv.name,
                            'partner_name': inv.partner_id.name,
                            'journal_name': pmt.journal_id.name,
                            'source_type':  'invoice',
                            'amount':        pmt.amount,
                        })
                else:
                    for pmt in inv.pos_order_ids.payment_ids:
                        lines.append({
                            'wizard_id':    self.id,
                            'branch_name':  branch.name,
                            'date':         pmt.pos_order_id.date_order.date(),
                            'invoice_name': inv.name,
                            'partner_name': inv.partner_id.name,
                            'journal_name': pmt.payment_method_id.name,
                            'source_type':  'invoice_pos',
                            'amount':        pmt.amount,
                        })

            # ---- credit notes --------------------------------------------
            for inv in credits:
                payments = inv._get_reconciled_payments()
                if payments:
                    for pmt in payments:
                        lines.append({
                            'wizard_id':    self.id,
                            'branch_name':  branch.name,
                            'date':         pmt.date,
                            'invoice_name': inv.name,
                            'partner_name': inv.partner_id.name,
                            'journal_name': pmt.journal_id.name,
                            'source_type':  'credit',
                            'amount':       -pmt.amount,
                        })
                else:
                    for pmt in inv.pos_order_ids.payment_ids:
                        lines.append({
                            'wizard_id':    self.id,
                            'branch_name':  branch.name,
                            'date':         pmt.pos_order_id.date_order.date(),
                            'invoice_name': inv.name,
                            'partner_name': inv.partner_id.name,
                            'journal_name': pmt.payment_method_id.name,
                            'source_type':  'credit_pos',
                            'amount':        pmt.amount,
                        })

            # ---- sale-order payments -------------------------------------
            for pmt in order_payments:
                signed = -pmt.amount if pmt.payment_type == 'outbound' else pmt.amount
                lines.append({
                    'wizard_id':        self.id,
                    'branch_name':      branch.name,
                    'date':             pmt.date,
                    'sale_order_name':  pmt.sale_order_id.name,
                    'partner_name':     pmt.sale_order_id.partner_id.name,
                    'journal_name':     pmt.journal_id.name,
                    'source_type':      'order_payment',
                    'amount':            signed,
                })

        self.env['payment.report'].create(lines)

        return {
            'type':      'ir.actions.act_window',
            'name':      'Payment Report',
            'res_model': 'payment.report',
            'view_mode': 'list,pivot',
            'domain':    [('wizard_id', '=', self.id)],
            'target':    'current',
        }

    # ------------------------------------------------------------------
    # Export Excel
    # ------------------------------------------------------------------

    def action_export_excel(self):
        self.ensure_one()

        output   = BytesIO()
        workbook = xlsxwriter.Workbook(output, {'in_memory': True})
        header   = workbook.add_format({'bold': True, 'border': 1, 'align': 'center'})
        text     = workbook.add_format({'border': 1})
        amount_fmt = workbook.add_format({'border': 1, 'align': 'center'})

        summary = workbook.add_worksheet('Summary')
        for col, h in enumerate([
            'Branch',
            'Invoices , Credits And Sales Order Count',
            'Total Invoices , Credits And Sales Order',
            'Total Payments',
            'Balance',
        ]):
            summary.write(0, col, h, header)
        summary_row = 1

        headers = ['Branch', 'Invoice Or Credit', 'Sale Order',
                   'Customer', 'Payment', 'Amount']

        for branch in self.branches_ids:
            invoices, credits, order_payments = self._get_branch_data(branch)

            sheet = workbook.add_worksheet(branch.name[:31])
            sheet.set_column(0, 5, 30)
            for col, h in enumerate(headers):
                sheet.write(0, col, h, header)

            row           = 1
            total_invoice = total_payment = 0.0

            for inv in invoices:
                payments = inv._get_reconciled_payments()
                if payments:
                    total_invoice += inv.amount_total
                    for pmt in payments:
                        sheet.write_row(row, 0, [
                            branch.name, inv.name, '',
                            inv.partner_id.name,
                            pmt.journal_id.name,
                            pmt.amount,
                        ], text)
                        total_payment += pmt.amount
                        row += 1
                else:
                    if inv.pos_order_ids.payment_ids:
                        total_invoice += inv.amount_total
                    for pmt in inv.pos_order_ids.payment_ids:
                        sheet.write_row(row, 0, [
                            branch.name, inv.name, '',
                            inv.partner_id.name,
                            pmt.payment_method_id.name,
                            pmt.amount,
                        ], text)
                        total_payment += pmt.amount
                        row += 1

            for inv in credits:
                payments = inv._get_reconciled_payments()
                if payments:
                    total_invoice -= inv.amount_total
                    for pmt in payments:
                        sheet.write_row(row, 0, [
                            branch.name, inv.name, '',
                            inv.partner_id.name,
                            pmt.journal_id.name,
                            -pmt.amount,
                        ], text)
                        total_payment -= pmt.amount
                        row += 1
                else:
                    if inv.pos_order_ids.payment_ids:
                        total_invoice -= inv.amount_total
                    for pmt in inv.pos_order_ids.payment_ids:
                        sheet.write_row(row, 0, [
                            branch.name, inv.name, '',
                            inv.partner_id.name,
                            pmt.payment_method_id.name,
                            pmt.amount,
                        ], text)
                        total_payment += pmt.amount
                        row += 1

            for pmt in order_payments:
                signed = -pmt.amount if pmt.payment_type == 'outbound' else pmt.amount
                total_invoice += signed
                sheet.write_row(row, 0, [
                    branch.name, '',
                    pmt.sale_order_id.name,
                    pmt.sale_order_id.partner_id.name,
                    pmt.journal_id.name,
                    signed,
                ], text)
                row += 1

            summary.set_column(0, 5, 30)
            summary.write_row(summary_row, 0, [
                branch.name,
                len(invoices) + len(credits) + len(order_payments),
                total_invoice,
                total_payment,
                total_invoice - total_payment,
            ], amount_fmt)
            summary_row += 1

        workbook.close()
        output.seek(0)

        self.file_name  = f'invoices_{datetime.today().date()}.xlsx'
        self.excel_file = base64.b64encode(output.read())

        return {
            'type':      'ir.actions.act_window',
            'res_model': 'account.branch.report.wizard',
            'res_id':    self.id,
            'view_mode': 'form',
            'target':    'new',
        }

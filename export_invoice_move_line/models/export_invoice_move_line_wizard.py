# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError
import base64
import io
import xlsxwriter


class ExportInvoiceMoveLineWizard(models.TransientModel):
    _name = "export.invoice.move.line.wizard"
    _description = "Export Account Move Lines for Invoices"

    branch_ids = fields.Many2many(
        'res.branch',
        'export_invoice_move_line_wizard_branch_rel',
        'wizard_id',
        'branch_id',  # ✅ Correct foreign key column name
        string="Branches"
    )

    sales_rep_ids = fields.Many2many('sales.rep','wizard_id', 'sales_rep_id', 'export_invoice_move_line_wizard_sales_rep_rel',string='Sales Rep',  )
    payment_journal_ids = fields.Many2many('account.journal', string="Payment Journal",  domain="[('type', 'in',['bank','cash'])]")

    categ_ids = fields.Many2many(
        comodel_name='product.category',
        string='Product Categories')

    family_ids = fields.Many2many(
        comodel_name='product.family',
        string='Product Families')

    discount_id = fields.Many2one(
        comodel_name='discount.reason',
        string='Discount Reason',
        required=False)

    vendor_id = fields.Many2one(
        comodel_name='res.partner',
        string='Vendor',
        required=False)
    date_from = fields.Date(string="From Date", required=True)
    date_to = fields.Date(string="To Date", required=True)

    file_data = fields.Binary('Excel File', readonly=True)
    file_name = fields.Char('File Name', readonly=True)

    def get_credit_note(self, invoice):
        credit = self.env['account.move'].search(
            [('reversed_entry_id', '=', invoice.id)], limit=1)

        return credit.name if credit else ''

    def action_export_excel(self):
        domain = [
            ('move_id.move_type', 'in', ['out_invoice','out_refund']), ('move_id.status_in_payment', 'in', ['paid', 'partial','in_payment','reversed']),
            ('move_id.state', '=', 'posted'),('display_type', 'in', ['product', 'line_section', 'line_note'])
        ]

        if self.branch_ids:
            domain.append(('move_id.branch_id', 'in', self.branch_ids.ids))
        if self.sales_rep_ids:
            domain.append(('move_id.sales_rep_id', 'in', self.sales_rep_ids.ids))
        if self.categ_ids:
            domain.append(('product_id.categ_id', 'in', self.categ_ids.ids))
        if self.family_ids:
            domain.append(('product_id.family_id', 'in', self.family_ids.ids))
        if self.vendor_id:
            domain.append(('product_id.vendor_id', '=', self.vendor_id.id))
        if self.discount_id:
            domain.append(('move_id.discount_id', '=', self.discount_id.id))
        if self.date_from:
            domain.append(('invoice_date', '>=', self.date_from))
        if self.date_to:
            domain.append(('invoice_date', '<=', self.date_to))

        invoice_lines = self.env['account.move.line'].search(domain, order="invoice_date desc")

        if not invoice_lines:
            raise UserError(_("No invoices found for the selected criteria."))

        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output, {'in_memory': True})
        sheet = workbook.add_worksheet("Invoice Move Lines")
        sheet.set_column(0,40,30)
        header_format = workbook.add_format({'bold': True, 'bg_color': '#D9E1F2'})
        currency_format = workbook.add_format({'num_format': '#,##0.00'})
        row = 0
        invoices = []

        if self.env.user.has_group('export_invoice_move_line.group_export_invoice_move_line_admin'):
            headers = ['Date','Branch','Ref','Credit','Opportunity','Customer Name','Customer Mobile','Customer Phone',
                       'Product Category','Family','UPC','Item Code','Description' ,'Quantity','Serial','Unit Cost',
                      'Unit Price', 'Discount (%)','Amount signed','Price Total Signed','Invoice Amount signed','Total Cost' ,'Invoice Price Total Signed','Payment Journals'
                        ,'Payment Amount Journals','Sales rep','PO','Vendor','Point','Channel','currency']
            for col, h in enumerate(headers):
                sheet.write(row, col, h, header_format)

            row += 1
            for line in invoice_lines:
                if self.payment_journal_ids:
                    if self.payment_journal_ids.ids not in line.move_id._get_reconciled_payments().mapped("journal_id").ids:
                        continue

                payments = line.move_id._get_reconciled_payments()
                if payments:
                    payment_journals = ", ".join(payments.mapped("journal_id.name"))
                    payment_amount = sum(payments.mapped("amount"))
                else:
                    payment_journals = ", ".join(line.move_id.pos_order_ids.payment_ids.mapped("payment_method_id.name"))
                    payment_amount = sum(line.move_id.pos_order_ids.payment_ids.mapped("amount"))





                total =  line.move_id.amount_total
                amount_total_signed =  line.move_id.amount_total_signed
                amount_untaxed_signed =  line.move_id.amount_untaxed_signed
                credit = self.get_credit_note(line.move_id)
                list_lots =line.move_id._get_invoiced_lot_values()
                serials =''
                for dic_lots in list_lots:
                    if dic_lots['product_name'] in[ line.product_id.display_name,line.product_id.name]:
                        serials += str(dic_lots['lot_name'])+" , "


                sheet.write(row, 0, str( line.move_id.invoice_date or ''))
                sheet.write(row, 1,  line.move_id.branch_id.name or '')
                sheet.write(row, 2,  line.move_id.name or '')
                sheet.write(row, 3, credit or '')
                sheet.write(row, 4, line.move_id.opportunity_id.name or '')
                sheet.write(row, 5, line.move_id.partner_id.name)
                sheet.write(row, 6, line.move_id.partner_id.mobile or '')
                sheet.write(row, 7, line.move_id.partner_id.phone or '')
                sheet.write(row, 8, line.product_id.categ_id.name or '')
                sheet.write(row, 9, line.product_id.product_tmpl_id.family_id.name or '')
                sheet.write(row, 10, line.product_id.default_code or '')
                sheet.write(row, 11, line.product_id.barcode or '')
                sheet.write(row, 12, line.product_id.display_name or '')
                sheet.write(row, 13, line.quantity if line.move_id.move_type != 'out_refund' else line.quantity *-1 or '')
                sheet.write(row, 14, serials or '')
                sheet.write(row, 15, float(line.product_id.standard_price) or 0)
                sheet.write(row, 16, float(line.product_id.lst_price) or 0)
                sheet.write(row, 17, line.discount or 0)
                sheet.write(row, 18, line.price_subtotal if line.move_id.move_type != 'out_refund' else  line.price_subtotal*-1 or 0)
                sheet.write(row, 19, line.price_total  if line.move_id.move_type != 'out_refund' else  line.price_total*-1 or 0)
                sheet.write(row, 20, amount_untaxed_signed if line.move_id.id not in invoices  else 0)
                sheet.write(row, 21, float(line.product_id.standard_price * line.quantity) if line.move_id.move_type != 'out_refund' else  float(line.product_id.standard_price * line.quantity) *-1 or 0)
                sheet.write(row, 22,amount_total_signed if line.move_id.id not in invoices  else 0)
                sheet.write(row, 23, payment_journals if line.move_id.id not in invoices  else '')
                sheet.write(row, 24, payment_amount if line.move_id.id not in invoices  else 0)
                sheet.write(row, 25, line.move_id.sales_rep_id.name or '')
                sheet.write(row, 26, line.move_id.reference_number or '')
                sheet.write(row, 27, line.product_id.vendor_id.name or '')
                sheet.write(row, 28, line.product_point or '')
                sheet.write(row, 29, line.move_id.channel_id.name or '')
                sheet.write(row, 30, line.move_id.currency_id.name or '')
                invoices.append(line.move_id.id)
                row += 1
        elif self.env.user.has_group('export_invoice_move_line.group_export_invoice_move_line_manager'):
            headers = ['Date', 'Branch', 'Ref', 'Credit', 'Opportunity', 'Customer Name', 'Customer Mobile',
                       'Customer Phone',
                       'Product Category', 'Family', 'UPC', 'Item Code', 'Description', 'Quantity', 'Serial',
                       'Unit Cost',
                       'Unit Price', 'Discount (%)','Amount signed','Price Total Signed', 'Invoice Amount signed', 'Total Cost', 'Invoice Price Total Signed',
                       'Payment Journals'
                , 'Payment Amount Journals', 'Sales rep', 'PO', 'Vendor',  'Channel', 'currency']
            for col, h in enumerate(headers):
                sheet.write(row, col, h, header_format)

            row += 1

            for line in invoice_lines:
                if self.payment_journal_ids:
                    if self.payment_journal_ids.ids not in line.move_id._get_reconciled_payments().mapped(
                            "journal_id").ids:
                        continue
                payments = line.move_id._get_reconciled_payments()
                payment_journals = ", ".join(payments.mapped("journal_id.name"))
                payment_amount = sum(payments.mapped("amount"))
                total = line.move_id.amount_total
                amount_total_signed = line.move_id.amount_total_signed
                amount_untaxed_signed = line.move_id.amount_untaxed_signed
                credit = self.get_credit_note(line.move_id)
                list_lots = line.move_id._get_invoiced_lot_values()
                serials = ''
                for dic_lots in list_lots:
                    if dic_lots['product_name'] == line.product_id.display_name:
                        serials += str(dic_lots['lot_name']) + " , "


                sheet.write(row, 0, str( line.move_id.invoice_date or ''))
                sheet.write(row, 1,  line.move_id.branch_id.name or '')
                sheet.write(row, 2,  line.move_id.name or '')
                sheet.write(row, 3, credit or '')
                sheet.write(row, 4, line.move_id.opportunity_id.name or '')
                sheet.write(row, 5, line.move_id.partner_id.name)
                sheet.write(row, 6, line.move_id.partner_id.mobile or '')
                sheet.write(row, 7, line.move_id.partner_id.phone or '')
                sheet.write(row, 8, line.product_id.categ_id.name or '')
                sheet.write(row, 9, line.product_id.product_tmpl_id.family_id.name or '')
                sheet.write(row, 10, line.product_id.default_code or '')
                sheet.write(row, 11, line.product_id.barcode or '')
                sheet.write(row, 12, line.product_id.display_name or '')
                sheet.write(row, 13, line.quantity  if line.move_id.move_type != 'out_refund' else line.quantity *-1 or '')
                sheet.write(row, 14, serials or '')
                sheet.write(row, 15, float(line.product_id.standard_price) or 0)
                sheet.write(row, 16, float(line.product_id.lst_price) or 0)
                sheet.write(row, 17, line.discount or 0)
                sheet.write(row, 18, line.price_subtotal  if line.move_id.move_type != 'out_refund' else  line.price_subtotal*-1 or 0)
                sheet.write(row, 19, line.price_total  if line.move_id.move_type != 'out_refund' else  line.price_total*-1 or 0)
                sheet.write(row, 20, amount_untaxed_signed if line.move_id.id not in invoices  else 0)
                sheet.write(row, 21,  float(line.product_id.standard_price * line.quantity) if line.move_id.move_type != 'out_refund' else  float(line.product_id.standard_price * line.quantity) *-1  or 0)
                sheet.write(row, 22,amount_total_signed if line.move_id.id not in invoices  else 0)
                sheet.write(row, 23, payment_journals if line.move_id.id not in invoices  else '')
                sheet.write(row, 24, payment_amount if line.move_id.id not in invoices  else 0)
                sheet.write(row, 25, line.move_id.sales_rep_id.name or '')
                sheet.write(row, 26, line.move_id.reference_number or '')
                sheet.write(row, 27, line.product_id.vendor_id.name or '')
                sheet.write(row, 28, line.move_id.channel_id.name or '')
                sheet.write(row, 29, line.move_id.currency_id.name or '')
                invoices.append(line.move_id.id)

                row += 1
        else:
            headers = ['Date', 'Branch', 'Ref', 'Credit', 'Opportunity', 'Customer Name', 'Customer Mobile',
                       'Customer Phone',
                       'Product Category', 'Family', 'UPC', 'Item Code', 'Description', 'Quantity', 'Serial',
                       'Unit Price', 'Discount (%)', 'Amount signed','Price Total Signed','Invoice Amount signed', 'Invoice Price Total Signed',
                       'Payment Journals'
                , 'Payment Amount Journals', 'Sales rep', 'PO','Point',  'Channel', 'currency']
            for col, h in enumerate(headers):
                sheet.write(row, col, h, header_format)

            row += 1

            for line in invoice_lines:
                if self.payment_journal_ids:
                    if self.payment_journal_ids.ids not in line.move_id._get_reconciled_payments().mapped(
                            "journal_id").ids:
                        continue
                payments = line.move_id._get_reconciled_payments()
                payment_journals = ", ".join(payments.mapped("journal_id.name"))
                payment_amount = sum(payments.mapped("amount"))
                total = line.move_id.amount_total
                amount_total_signed = line.move_id.amount_total_signed
                amount_untaxed_signed = line.move_id.amount_untaxed_signed
                credit = self.get_credit_note(line.move_id)
                list_lots = line.move_id._get_invoiced_lot_values()
                serials = ''
                for dic_lots in list_lots:
                    if dic_lots['product_name'] == line.product_id.display_name:
                        serials += str(dic_lots['lot_name']) + " , "

                sheet.write(row, 0, str(line.move_id.invoice_date or ''))
                sheet.write(row, 1, line.move_id.branch_id.name or '')
                sheet.write(row, 2, line.move_id.name or '')
                sheet.write(row, 3, credit)
                sheet.write(row, 4, line.move_id.opportunity_id.name)
                sheet.write(row, 5, line.move_id.partner_id.name)
                sheet.write(row, 6, line.move_id.partner_id.mobile)
                sheet.write(row, 7, line.move_id.partner_id.phone)
                sheet.write(row, 8, line.product_id.categ_id.name or '')
                sheet.write(row, 9, line.product_id.product_tmpl_id.family_id.name or '')
                sheet.write(row, 10, line.product_id.default_code or '')
                sheet.write(row, 11, line.product_id.barcode or '')
                sheet.write(row, 12, line.product_id.display_name or '')
                sheet.write(row, 13, line.quantity if line.move_id.move_type != 'out_refund' else line.quantity *-1 or '')
                sheet.write(row, 14, serials or '')
                sheet.write(row, 15, line.product_id.lst_price or '')
                sheet.write(row, 16, line.discount or '')
                sheet.write(row, 17, line.price_subtotal  if line.move_id.move_type != 'out_refund' else  line.price_subtotal*-1 or 0)
                sheet.write(row, 18, line.price_total if line.move_id.move_type != 'out_refund' else  line.price_total*-1 or 0)
                sheet.write(row, 19, amount_untaxed_signed if line.move_id.id not in invoices  else 0)
                sheet.write(row, 20, amount_total_signed if line.move_id.id not in invoices  else 0)
                sheet.write(row, 21, payment_journals if line.move_id.id not in invoices  else '')
                sheet.write(row, 22, payment_amount if line.move_id.id not in invoices  else 0)
                sheet.write(row, 23, line.move_id.sales_rep_id.name or '')
                sheet.write(row, 24, line.move_id.reference_number or '')
                sheet.write(row, 25, line.product_point or '')
                sheet.write(row, 26, line.move_id.channel_id.name or '')
                sheet.write(row, 27, line.move_id.currency_id.name or '')
                invoices.append(line.move_id.id)
                row += 1


        workbook.close()
        output.seek(0)
        data = output.read()
        output.close()

        filename = f"invoice_move_lines_{fields.Date.today()}.xlsx"
        self.write({
            'file_data': base64.b64encode(data),
            'file_name': filename,
        })

        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/?model={self._name}&id={self.id}&field=file_data&filename_field=file_name&download=true',
            'target': 'self',
        }

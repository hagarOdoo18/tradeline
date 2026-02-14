from odoo import models, fields, api
from io import BytesIO
import base64
import xlsxwriter


class AppleStockReportWizard(models.TransientModel):
    _name = 'apple.stock.report.wizard'
    _description = 'Apple Stock Quant Excel Report'

    gentextfile = fields.Binary('File')



    location_ids = fields.Many2many('stock.location',domain=lambda self: [('id', 'in',self.env.user.stock_location_ids.ids)],)

    def generate_xlsx_report(self):
        self.ensure_one()

        output = BytesIO()
        workbook = xlsxwriter.Workbook(output, {'in_memory': True})
        sheet = workbook.add_worksheet('Apple Stock Quant Sheet')

        header_format = workbook.add_format({
            'bold': True,
            'border': 1,
            'align': 'center',
            'valign': 'vcenter',
            'fg_color': '#2ecc71',
            'font_color': 'white',
        })
        sheet.set_column(0, 9, 20)  # A
        cell_format = workbook.add_format({'align': 'center'})

        is_admin = self.env.user.has_group(
            'apple_stock_report_excel.group_export_applestock_vendor'
        )

        headers = [
            'Store Name',
            'Apple Store id',
            'Family',
            'Category',
        ]

        if is_admin:
            headers += ['Vendor']

        headers += [
            'Item Code',
            'Product Name',
            'ON Hand'
        ]

        for col, header in enumerate(headers):
            sheet.write(0, col, header, header_format)

        # -----------------------------
        # Stock Quant domain
        # -----------------------------
        domain = [('company_id', '=', self.env.company.id)]
        if not  self.location_ids:
            domain += [('location_id.usage', '=', 'internal')]
        else:
            domain += [('location_id', 'in',  self.location_ids.ids)]


        quants = self.env['stock.quant'].search(domain).sorted(
            key=lambda q: (q.sudo().product_id.id, q.location_id.id)
        )

        row = 1
        processed_keys = set()

        for quant in quants:
            key = (
                quant.sudo().product_id.id,
                quant.location_id.id,
                quant.location_id.location_id.id if quant.location_id.location_id else False
            )

            if key in processed_keys:
                continue

            same_quants = self.env['stock.quant'].search([
                ('product_id', '=', quant.sudo().product_id.id),
                ('location_id', '=', quant.location_id.id),('company_id','=',self.env.company.id)
            ])

            quantity = sum(same_quants.mapped('quantity'))

            processed_keys.add(key)

            # Data

            store_name = quant.branch_id.name
            product = quant.product_id
            family = product.family_id.name or ''
            category = product.categ_id.name or ''
            vendor =product.vendor_id.name or ''
            item_code = product.barcode or ''
            product_name = product.display_name

            # Store ID
            store_id = ''

            store_id = quant.branch_id.apple_store_id

            col = 0
            sheet.write(row, col, store_name, cell_format); col += 1
            sheet.write(row, col, store_id, cell_format); col += 1
            sheet.write(row, col, family, cell_format); col += 1
            sheet.write(row, col, category, cell_format); col += 1

            if is_admin:
                sheet.write(row, col, vendor, cell_format); col += 1

            sheet.write(row, col, item_code, cell_format); col += 1
            sheet.write(row, col, product_name, cell_format); col += 1
            sheet.write(row, col, quantity, cell_format)

            row += 1

        workbook.close()
        output.seek(0)

        self.gentextfile = base64.b64encode(output.getvalue())

        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{self._name}/{self.id}/gentextfile/Apple_Stock_Quant.xlsx?download=true',
            'target': 'new',
        }

# -*- coding: utf-8 -*-

import base64
from datetime import datetime
from odoo import models, fields, api
from io import BytesIO
import xlsxwriter

class StockQuantInherit(models.TransientModel):
    _name = 'stock.abm.report.wizard'

    gentextfile = fields.Binary('Click On Save As Button To Download File', readonly=True)

    warehouse_id = fields.Many2one('stock.warehouse', string="Location",)

    date_from = fields.Date(
        string='Date From',
        required=False)

    date_to = fields.Date(
        string='Date To',
        required=False)


    def generate_sql(self):

        sql_qty = "Select DISTINCT rs.name,  pp.barcode , pt.name , sum(sq.quantity) as qty  FROM public.stock_quant as sq  " \
                  "LEFT JOIN product_product pp ON pp.id  = sq.product_id " \
                  "LEFT JOIN product_template pt ON pt.id  = pp.product_tmpl_id " \
                  "LEFT JOIN stock_production_lot spl ON spl.id  = sq.lot_id " \
                  "LEFT JOIN product_supplierinfo ps ON  ps.product_tmpl_id =  pt.id " \
                  "LEFT JOIN product_family pf ON  pf.id = pt.family_id " \
                  "LEFT JOIN res_partner rs ON  rs.id  = ps.name " \
                  " Where location_id = " + str(self.warehouse_id.lot_stock_id.id) + " and pp.active = True and pf.id = 133 and ps.name in (158445 ,173540) group by pp.barcode , pt.name , rs.name "

        teams= self.env['crm.team'].search([('location_ids','in',[self.warehouse_id.lot_stock_id.id]),('team_type','=','sales')])
        if len(teams.ids)> 1:
            team = self.env['crm.team'].browse(52)
        sql_moves = "SELECT  rs.name,pp.barcode , pt.name  , ail.quantity_signed FROM public.account_invoice_line ail " \
              "LEFT JOIN product_product pp ON pp.id  = ail.product_id " \
              "LEFT JOIN product_template pt ON pt.id  = pp.product_tmpl_id " \
              "LEFT JOIN product_supplierinfo ps ON  ps.product_tmpl_id =  pt.id " \
              "LEFT JOIN product_category pc ON  pc.id =  pt.categ_id " \
              "LEFT JOIN product_family pf ON  pf.id = pt.family_id " \
              "LEFT JOIN account_invoice ai ON  ai.id =  ail.invoice_id " \
              "LEFT JOIN res_partner rs ON  rs.id  = ai.partner_id " \
              "LEFT JOIN  crm_team ct on ct.id = ai.team_id " \
              " where ai.state not in ('draft','cancel') and ps.name in (158445 ,173540) and  ail.invoice_date >= '"+str(self.date_from)+"'  and pf.id = 133 and  ail.invoice_date <= '"+str(self.date_to)+"' and ct.name not in ('sales')  and ct.id in"+str(tuple(teams.ids))+"group by rs.name,pp.barcode , pt.name  , ail.quantity_signed"

        sql_moves2 = "SELECT  pp.barcode , pt.name  , sum(ail.quantity_signed) as QTY FROM public.account_invoice_line ail " \
                      "LEFT JOIN product_product pp ON pp.id  = ail.product_id " \
                      "LEFT JOIN product_template pt ON pt.id  = pp.product_tmpl_id " \
                      "LEFT JOIN product_supplierinfo ps ON  ps.product_tmpl_id =  pt.id " \
                      "LEFT JOIN product_category pc ON  pc.id =  pt.categ_id " \
                      "LEFT JOIN product_family pf ON  pf.id = pt.family_id " \
                      "LEFT JOIN account_invoice ai ON  ai.id =  ail.invoice_id " \
                      "LEFT JOIN  crm_team ct on ct.id = ai.team_id " \
                      " where ai.state not in ('draft','cancel') and ps.name in (158445 ,173540) and  ail.invoice_date >= '"+str(self.date_from)+"'  and pf.id = 133 and  ail.invoice_date <= '"+str(self.date_to)+"' and ct.name not in ('sales')  and ct.id in"+str(tuple(teams.ids))+"group by pp.barcode , pt.name  , ail.quantity_signed"


        return sql_qty,sql_moves,team.apple_store_id,team.name,sql_moves2

    @api.multi
    def generate_xlsx_report(self):
        output = BytesIO()
        workbook = xlsxwriter.Workbook(output, {'in_memory': True})
        sheet = workbook.add_worksheet('Stock Quantity Sheet')
        sheet2 = workbook.add_worksheet('Sold QTY')
        sheet3 = workbook.add_worksheet('Sum Sold QTY')
        without_borders = workbook.add_format({
            'bold': 1,
            'border': 1,
            'align': 'center',
            'valign': 'vcenter',
            'text_wrap': True,
            'font_size': '11',

        })

        font_size_10 = workbook.add_format(
            {'font_name': 'KacstBook', 'font_size': 10, 'align': 'center', 'valign': 'vcenter', 'text_wrap': True,
             'border': 1})

        table_header_formate = workbook.add_format({
            'bold': 1,
            'border': 1,
            'bg_color': '#AAB7B8',
            'font_size': '10',
            'align': 'center',
            'valign': 'vcenter',
            'text_wrap': True
        })

        sheet.set_column(0, 2, 30, without_borders)
        sheet.set_column(3, 3,70, without_borders)
        sheet.set_column(4, 5,20, without_borders)
        sheet.write('A1', 'Store', table_header_formate)
        sheet.write('B1', 'Apple Store Id', table_header_formate)
        sheet.write('C1', 'Item Code', table_header_formate)
        sheet.write('D1', 'Description', table_header_formate)
        sheet.write('E1', 'QTY', table_header_formate)
        sheet.write('F1', 'Vendor', table_header_formate)

        sheet2.set_column(0, 2, 30, without_borders)
        sheet2.set_column(3, 3,70, without_borders)
        sheet2.set_column(4, 4,20, without_borders)
        sheet2.write('A1', 'Store', table_header_formate)
        sheet2.write('B1', 'Customer Name', table_header_formate)
        sheet2.write('C1', 'Item Code', table_header_formate)
        sheet2.write('D1', 'Description', table_header_formate)
        sheet2.write('E1', 'QTY', table_header_formate)

        sheet3.set_column(0, 1, 30, without_borders)
        sheet3.set_column(2, 2,70, without_borders)
        sheet3.set_column(3, 3,20, without_borders)
        sheet3.write('A1', 'Store', table_header_formate)
        sheet3.write('B1', 'Item Code', table_header_formate)
        sheet3.write('C1', 'Description', table_header_formate)
        sheet3.write('D1', 'QTY', table_header_formate)

        sql_qty,sql_moves,apple_store,store ,sql_moves1= self.generate_sql()
        self.env.cr.execute(sql_qty)
        store_qtys = self.env.cr.fetchall()
        self.env.cr.execute(sql_moves)
        store_moves = self.env.cr.fetchall()
        self.env.cr.execute(sql_moves1)
        store_moves1 = self.env.cr.fetchall()
        row = 1
        col = 0
        for qty in store_qtys:
            sheet.write(row, col , store or '', font_size_10)
            sheet.write(row, col +1 , apple_store or '', font_size_10)
            sheet.write(row, col +2, qty[1] or '', font_size_10)
            sheet.write(row, col +3, qty[2] or '', font_size_10)
            sheet.write(row, col +4, qty[3] or '', font_size_10)
            sheet.write(row, col +5, qty[0] or '', font_size_10)
            row += 1
        row = 1
        col = 0
        for qty in store_moves:
            sheet2.write(row, col , store or '', font_size_10)
            sheet2.write(row, col +1, qty[0] or '', font_size_10)
            sheet2.write(row, col +2, qty[1] or '', font_size_10)
            sheet2.write(row, col +3, qty[2] or '', font_size_10)
            sheet2.write(row, col +4, qty[3] or '', font_size_10)
            row += 1
        row = 1
        col = 0
        for qty in store_moves1:
            sheet3.write(row, col , store or '', font_size_10)
            sheet3.write(row, col +1, qty[0] or '', font_size_10)
            sheet3.write(row, col +2, qty[1] or '', font_size_10)
            sheet3.write(row, col +3, qty[2] or '', font_size_10)
            row += 1

        workbook.close()
        output.seek(0)
        self.write({'gentextfile': base64.encodestring(output.getvalue())})

        return {
            'type': 'ir.actions.act_url',
            'name': 'Stock Quant Sheet',
            'url': '/web/content/stock.abm.report.wizard/%s/gentextfile/Stock Quant Sheet.xlsx?download=true' % (self.id),
            'target': 'new'
        }













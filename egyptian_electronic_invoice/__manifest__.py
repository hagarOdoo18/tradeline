# -*- coding: utf-8 -*-
#################################################################################
#
#    Odoo, Open Source Management Solution
#    Copyright (C) 2017-today Ascetic Business Solution <www.asceticbs.com>
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
#################################################################################

{
    'name': "ETA - Egyptian Electronic Invoice V15.0",
    'author': 'Ahmed Salama',
    'category': 'Accounting',
    'summary': """Egyptian Electronic Invoice Submitting module V15.0""",
    'website': 'https://www.linkedin.com/in/ahmed-salama-a982b182/',
    'license': 'AGPL-3',
    'description': """Egyptian Electronic Invoice Submitting module V15.0""",
    'version': '18.1',
    'depends': ['account', 'product', 'uom'],
    'data': [
        # 'data/data_files.xml',
        # 'data/data_electronic_invoice.xml',
        # 'data/data_taxes.xml',
        # # 'data/data_product.xml',
        #
        # 'security/e_invoice_security.xml',
        # 'security/ir.model.access.csv',
        #
        # 'views/product_template_view_changes.xml',
        # 'views/res_config_settings_view_changes.xml',
        # 'views/res_company_view_changes.xml',
        # 'views/res_partner_view_changes.xml',
        # 'views/account_move_view_changes.xml',
        # 'views/account_tax_view_changes.xml',
        # 'views/vendor_received_document_view.xml',
        # 'views/uom_view_changes.xml',
        #
        # 'wizard/electronic_invoice_result_view.xml',
        # 'wizard/link_bill_view.xml',
    ],
    # 'demo': ['data/e_invoice_demo.xml'],
    # 'images': ['thumbnail.jpg', 'thumbnail.png', 'banner.png'],
    'installable': True,
    'application': True,
    'auto_install': False,
    'price': 999.99,
    'currency': 'USD',
}

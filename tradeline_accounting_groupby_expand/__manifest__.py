# -*- coding: utf-8 -*-
{
    "name": "Tradeline Accounting GroupBy Expand",
    "summary": "Expand Group By options for targeted accounting reports",
    "version": "18.0.1.8.4",
    "category": "Accounting",
    "author": "Tradeline",
    "license": "LGPL-3",
    "depends": [
        "web",
        "accounting_customization",
        "sales_invoice_lines_view",
    ],
    "data": [
        "views/account_invoice_report_search.xml",
        "views/account_move_line_sales_search.xml",
        "views/account_invoice_report_pivot.xml",
        "views/account_invoice_report_list.xml",
        "data/time_range_sync.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "tradeline_accounting_groupby_expand/static/src/js/hide_custom_groupby.js",
            "tradeline_accounting_groupby_expand/static/src/js/time_ranges_menu.js",
            "tradeline_accounting_groupby_expand/static/src/xml/time_ranges.xml",
            "tradeline_accounting_groupby_expand/static/src/scss/time_ranges.scss",
        ],
    },
    "post_init_hook": "post_init_hook",
    "installable": True,
    "application": False,
    "auto_install": False,
}

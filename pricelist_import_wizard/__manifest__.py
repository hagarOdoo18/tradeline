{
    'name': 'Pricelist Import Wizard',
    'version': '18.0.2.0.0',
    'summary': 'Import pricelist items from an Excel/CSV sheet',
    'description': """
        Wizard to import product.pricelist.item records from a spreadsheet.

        Supports every option of both driving fields:

          applied_on    : 3_global, 2_product_category, 1_product, 0_product_variant
          compute_price : fixed, percentage, formula (with base: Sales Price /
                          Cost / Other Pricelist)

        The expected sheet columns follow the chosen combination:
          - target  : item_code (product / variant) or category (category level);
                      no target column for All Products
          - value   : fixed_price | percent_price | price_discount,
                      price_surcharge, price_round, price_min_margin,
                      price_max_margin
          - optional: min_quantity

        Currency and company are inherited from the pricelist. A preview step
        resolves every row and reports warnings and errors before committing.
    """,
    'category': 'Sales/Sales',
    'author': 'Tradeline',
    'depends': ['product', 'sale'],
    'data': [
        'security/ir.model.access.csv',
        'views/pricelist_import_wizard_view.xml',
        'views/product_pricelist_view.xml',
    ],
    'installable': True,
    'auto_install': False,
    'license': 'LGPL-3',
}

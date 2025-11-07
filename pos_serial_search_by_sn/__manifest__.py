{
    "name": "POS Serial Search by SN",
    "version": "18.0.1.0",
    "category": "Point of Sale",
    "summary": "Search products in POS by Serial Number (Lot)",
    "author": "ChatGPT for Ezzat",
    "license": "LGPL-3",
    "depends": ["point_of_sale", "stock"],
    "assets": {
        "point_of_sale._assets_pos": [
            "pos_serial_search_by_sn/static/src/js/serial_loader.js",
            "pos_serial_search_by_sn/static/src/js/serial_button.js",
            "pos_serial_search_by_sn/static/src/js/serial_search_patch.js"
        ]
    }
}

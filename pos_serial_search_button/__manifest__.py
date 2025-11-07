{
    "name": "POS Serial Search Button",
    "version": "18.0.1.0.0",
    "summary": "Add button in POS to search products by Serial/Lot number",
    "author": "Your Name",
    "website": "https://yourcompany.com",
    "depends": ["point_of_sale", "stock"],
    "data": [
        "static/src/xml/serial_button.xml"
    ],
    "data": [],
    "assets": {
        "point_of_sale._assets_pos": [
            "pos_serial_search_button/static/src/js/pos_serial_search.js",
            # "pos_serial_search_button/static/src/xml/serial_button.xml",
        ],
    },
    "installable": True,
    "application": False
}

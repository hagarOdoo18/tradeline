{
    "name": "Product Upload from Excel",
    "summary": "Upload Products and Variants from Excel file",
    "version": "18.0.1.0.0",
    "author": "Your Name",
    "website": "http://yourcompany.com",
    "category": "Product",
    "depends": ["stock"],
    "data": [
        'security/ir.model.access.csv',
        "views/product_upload_excel_views.xml",
    ],
    "installable": True,
    "application": False,
    "license": "LGPL-3"
}
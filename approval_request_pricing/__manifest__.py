{
    "name": "Approval Request Pricing",
    "version": "18.0.1.0.0",
    "category": "Human Resources/Approvals",
    "summary": "Add payment details and product pricing to RFQ approvals",
    "author": "Tradeline",
    "license": "LGPL-3",
    "depends": [
        "account",
        "approvals_purchase",
    ],
    "data": [
        "views/approval_request_views.xml",
        "views/approval_product_line_views.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}

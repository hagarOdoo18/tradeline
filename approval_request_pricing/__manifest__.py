{
    "name": "Approval Request Pricing",
    "version": "18.0.1.1.0",
    "category": "Human Resources/Approvals",
    "summary": "Add standalone payment and pricing inputs to RFQ approvals",
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

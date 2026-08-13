{
    "name": "Approval Request Pricing",
    "version": "18.0.1.2.1",
    "category": "Human Resources/Approvals",
    "summary": "Add configurable payment choices and automatic RFQ pricing",
    "author": "Tradeline",
    "license": "LGPL-3",
    "depends": [
        "account",
        "approvals_purchase",
    ],
    "data": [
        "security/ir.model.access.csv",
        "views/approval_pricing_option_views.xml",
        "views/approval_request_views.xml",
        "views/approval_product_line_views.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}

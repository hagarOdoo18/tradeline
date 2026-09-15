## Module <shopify_odoo_connector>

#### 01.04.2025
#### Version 18.0.1.0.0
#### ADD
- Initial commit for Shopify Odoo Connector

#### 01.10.2025
#### Version 18.0.1.0.1
#### UPDT
- Fixed the Dashboard issue while opening the module

#### 16.03.2026
#### Version 18.0.1.0.2
#### UPDT
- Automated the token generation inside the instance configuration

#### 15.09.2026
#### Version 18.0.1.0.5
#### UPDT
- Confirmed-order API (`create_confirmed_order`) now creates orders through the
  same cycle as `import_confirmed_orders_from_shopify` (called with a
  one-order page)

#### 15.09.2026
#### Version 18.0.1.0.6
#### UPDT
- Confirmed-order API: the Order API Key field and its Generate button are
  replaced by `POST /api/shopify/v1/auth` (Store Name + Client Secret -> Bearer
  token valid 24 hours, stored hashed in `shopify.api.token`)

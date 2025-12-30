# -*- coding: utf-8 -*-
#################################################################################
# Author      : Webkul Software Pvt. Ltd. (<https://webkul.com/>)
# Copyright(c): 2015-Present Webkul Software Pvt. Ltd.
# All Rights Reserved.
#
#
#
# This program is copyright property of the author mentioned above.
# You can`t redistribute it and/or modify it.
#
#
# You should have received a copy of the License along with this program.
# If not, see <https://store.webkul.com/license.html/>
#################################################################################
{
  "name"                 :  "POS Scan Product By Lot/Serial Number",
  "summary"              :  """Allows  the POS user to add the products by scanning their Lot/Serial Number.Scan Lot|Serial Number Scan|POS Lot Number Scan|Add Lot Item Scan.|Pos Product By Lot""",
  "category"             :  "Point Of Sale",
  "version"              :  "1.0.2",
  "sequence"             :  1,
  "author"               :  "Webkul Software Pvt. Ltd.",
  "license"              :  "Other proprietary",
  "website"              :  "http://www.webkul.com",
  "description"          :  """Pos add product by lot, Pos add product by serial number, Lot number scanner, Serial number scanner
                                , Scan product by lot number, Scan product by serial number""",
  "live_test_url"        :  "http://odoodemo.webkul.com/?module=pos_product_by_lot_number&custom_url=/pos/auto",
  "depends"              :  ['point_of_sale'],
  "demo"                 :  ['data/product_by_lot_demo.xml'],
  "images"               :  ['static/description/banner.png'],
  "application"          :  True,
  "installable"          :  True,
  "assets"               :  {
                              'point_of_sale._assets_pos': [
                                'pos_product_by_lot_number/static/src/overrides/product_screen/order_summary/order_summary.js',
                                'pos_product_by_lot_number/static/src/overrides/models/pos_order.js',
                                'pos_product_by_lot_number/static/src/overrides/product_screen/product_screen.js',
                                'pos_product_by_lot_number/static/src/overrides/payment_screen/payment_screen.js',
                                'pos_product_by_lot_number/static/src/overrides/select_lot_popup/select_lot_popup.js',
                                'pos_product_by_lot_number/static/src/overrides/select_lot_popup/pos_product_by_lot_number.xml',
                                'pos_product_by_lot_number/static/src/overrides/select_lot_popup_inh/pos_product_by_lot_number_search.xml',
                                'pos_product_by_lot_number/static/src/overrides/select_lot_popup_inh/select_lot_popup_search.js',
                              ],
                            },
  "auto_install"         :  False,
  "price"                :  69,
  "currency"             :  "USD",
  "pre_init_hook"        :  "pre_init_check",
}

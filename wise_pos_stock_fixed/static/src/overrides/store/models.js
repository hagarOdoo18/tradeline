/** @odoo-module */
import { patch } from '@web/core/utils/patch';
import { _t } from '@web/core/l10n/translation';
import { getProductQty } from '../../utils';
import { PosOrderline } from "@point_of_sale/app/models/pos_order_line";

patch(PosOrderline.prototype, {
  set_quantity(quantity, keep_price) {
    const config = this.models['pos.config'].getFirst();
    const productQty = getProductQty(config.stock_type, this.product_id);

    if (
      config.is_restrict_out_of_stock_products &&
      productQty < quantity
    ) {
      // Return error info — the caller (OrderWidget) shows this as a dialog
      return {
        title: _t('Insufficient Stock'),
        body: _t(
          'The quantity entered exceeds the available stock. Please enter a quantity less than or equal to the available stock.'
        ),
      };
    }

    return super.set_quantity(quantity, keep_price);
  },
});

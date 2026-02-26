/** @odoo-module */
import { patch } from '@web/core/utils/patch';
import { PosStore } from '@point_of_sale/app/store/pos_store';
import { handleRPCError } from "@point_of_sale/app/errors/error_handlers";
import { _t } from '@web/core/l10n/translation';
import { getProductQty } from '../../utils';
import { usePos } from '@point_of_sale/app/store/pos_hook';
import { useService } from "@web/core/utils/hooks";

import { PosOrderline } from "@point_of_sale/app/models/pos_order_line";
import { AlertDialog } from "@web/core/confirmation_dialog/confirmation_dialog";

patch(PosOrderline.prototype, {

  set_quantity(quantity, keep_price) {
    console.log(this,"...............this");
    var config = this.models["pos.config"].getFirst()
    const productQty = getProductQty(config.stock_type, this.product_id);

    if (
      productQty < quantity &&
      config.is_restrict_out_of_stock_products

    ) {
        return {
            title: _t("Insufficient Stock"),
            body: _t(
                "The quantity entered exceeds the available stock. Please enter a quantity less than or equal to the available stock."
            ),
        };
    } else {
      return super.set_quantity(quantity, keep_price);
    }
  },
});

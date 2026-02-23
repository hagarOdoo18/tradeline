/** @odoo-module */
import { patch } from '@web/core/utils/patch';
import { PosStore } from '@point_of_sale/app/store/pos_store';
import { getProductQty } from '../../utils';
import { handleRPCError } from "@point_of_sale/app/errors/error_handlers";
import { _t } from '@web/core/l10n/translation';
import { AlertDialog } from "@web/core/confirmation_dialog/confirmation_dialog";

patch(PosStore.prototype, {
  async addProductToCurrentOrder(product, options = {}) {
    // `this.config` — NOT `this.pos.config` (PosStore IS the pos store)
    const productQty = getProductQty(this.config.stock_type, product);
    if (productQty <= 0 && this.config.is_restrict_out_of_stock_products) {
      return;
    }
    return super.addProductToCurrentOrder(product, options);
  },

  async pay() {
    const currentOrder = this.get_order();  // NOT this.pos.get_order()
    if (
      currentOrder &&
      currentOrder.lines.length > 0 &&
      this.config.is_restrict_out_of_stock_products
    ) {
      const outOfStockProducts = [];
      currentOrder.lines.forEach((line) => {
        const qty = getProductQty(this.config.stock_type, line.product_id);
        if (qty < line.qty) {
          outOfStockProducts.push(line.product_id.display_name);
        }
      });

      if (outOfStockProducts.length > 0) {
        this.env.services.dialog.add(AlertDialog, {
          title: _t('Insufficient Stock'),
          body: _t(
            `The quantity entered exceeds the available stock. Please reduce the quantity. [${outOfStockProducts.join(', ')}]`
          ),
        });
        return;
      }
    }
    return super.pay(...arguments);
  },
});

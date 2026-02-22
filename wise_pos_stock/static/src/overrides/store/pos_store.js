/** @odoo-module */
import { patch } from '@web/core/utils/patch';
import { PosStore } from '@point_of_sale/app/store/pos_store';
import { getProductQty } from '../../utils';
import { handleRPCError } from "@point_of_sale/app/errors/error_handlers";
import { _t } from '@web/core/l10n/translation';


patch(PosStore.prototype, {
  async addProductToCurrentOrder(product, options = {}) {
    const productQty = getProductQty(this.config.stock_type, product);
    if (productQty <= 0 && this.config.is_restrict_out_of_stock_products) {
      return;
    }
    super.addProductToCurrentOrder(product, (options = {}));
  },

  async pay() {
    const currentOrder = this.get_order();
    if (
      currentOrder.length > 0 &&
      this.pos.config.is_restrict_out_of_stock_products
    ) {
      const outOfStockProducts = [];
      currentOrder.forEach((line) => {
        let qty = getProductQty(this.pos.config.stock_type, line.product);
        if (qty < line.quantity) {
          outOfStockProducts.push(line.product.display_name);
        }
      });
      if (outOfStockProducts && outOfStockProducts.length > 0) {
        this.env.services.popup.add(handleRPCError, {
          title: _t('Insufficient Stock'),
          body: _t(
            `The quantity entered exceeds the available stock. Please enter a quantity less than or equal to the available stock. [${outOfStockProducts.join(
              ','
            )}]`
          ),
        });
        return;
      }
    }
    return super.pay(...arguments);
  },

});

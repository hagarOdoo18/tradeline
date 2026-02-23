/** @odoo-module */
import { patch } from '@web/core/utils/patch';
import { _t } from '@web/core/l10n/translation';
import { PaymentScreen } from '@point_of_sale/app/screens/payment_screen/payment_screen';
import { handleRPCError } from "@point_of_sale/app/errors/error_handlers";
import { getProductQty } from '../../../utils';
patch(PaymentScreen.prototype, {
  async validateOrder() {
    if (
      this.currentOrder.lines.length > 0 &&
      this.pos.config.is_restrict_out_of_stock_products
    ) {
      const outOfStockProducts = [];
      this.currentOrder.lines.forEach((line) => {
        let qty = getProductQty(this.pos.config.stock_type, line.product_id);

        if (qty < line.quantity) {
          outOfStockProducts.push(line.product_id.display_name);
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
    return super.validateOrder(...arguments);
  },
  async _finalizeValidation() {
    //if (this.pos.config.update_stock_quantities === 'real') {
    this.currentOrder.lines.forEach((line) => {
        console.log(line,"...............line111");
      line.product_id.qty_available -= line.qty;
      line.product_id.virtual_available -= line.qty;
    });

    return super._finalizeValidation(...arguments);
  },
});

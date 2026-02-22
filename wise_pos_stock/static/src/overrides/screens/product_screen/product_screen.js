/** @odoo-module */
import { onMounted, onWillUnmount } from '@odoo/owl';
import {
  ConnectionLostError,
  ConnectionAbortedError,
} from '@web/core/network/rpc';
import { _t } from '@web/core/l10n/translation';
import { patch } from '@web/core/utils/patch';
import { handleRPCError } from "@point_of_sale/app/errors/error_handlers";
import { ProductScreen } from '@point_of_sale/app/screens/product_screen/product_screen';

patch(ProductScreen.prototype, {
  setup() {
    super.setup(...arguments);

    onMounted(() => {
      if (this.pos.config.update_stock_quantities === 'real') {
        const refreshRate = this.pos.config.stock_quantities_refresh_rate;
        const refreshRateMS = refreshRate && refreshRate * 60000;
        this._runUpdateProductQtyTimer(refreshRateMS);
        this.loadLatestProductQtyFromDB();
      }
    });
    onWillUnmount(() => {
      if (this.pos.config.update_stock_quantities === 'real') {
        clearInterval(this.updateProductQtyTimer);
      }
    });
  },
  _runUpdateProductQtyTimer(time = 180000) {
    this.updateProductQtyTimer = setInterval(() => {
      this.loadLatestProductQtyFromDB();
    }, time);
  },

  async loadLatestProductQtyFromDB() {
    try {
      let kwargs = {};
      var all_products = this.pos.models["product.product"].getAll();
      const productIds = Object.keys(all_products);
      var data = []
      for (let key in all_products) {
        data.push(all_products[key].id)
      }
      console.log(data,"..............products");
      if (this.pos.config.stock_warehouse === 'current') {
        console.log(this.pos.config.picking_type_id.default_location_src_id.id);
        kwargs.context = {
          location: this.pos.config.picking_type_id.default_location_src_id.id,
        };
      }
      const products = await this.pos.data.searchRead(
        'product.product',
        [['id', 'in', data]],
        ['qty_available', 'virtual_available'],
        kwargs
      );
      console.log(this,"..............products");
      if (products && products.length > 0) {
        products.forEach((product) => {
          if (all_products[product.id]) {
            this.pos.models['product.product'].get(product.id).qty_available =
              product.qty_available;
             this.pos.models['product.product'].get(product.id).virtual_available =
              product.virtual_available;
          }
        });
      }
    } catch (error) {
      if (
        error instanceof ConnectionLostError ||
        error instanceof ConnectionAbortedError
      ) {
        //TODO: Change Message
        return this.popup.add(handleRPCError, {
          title: _t('Network Error'),
          body: _t(
            'Product is not loaded. Tried loading the product from the server but there is a network error.'
          ),
        });
      } else {
        throw error;
      }
    }
  },
});
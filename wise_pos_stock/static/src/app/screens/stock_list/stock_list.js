/** @odoo-module **/

import { registry } from '@web/core/registry';
import { useService, useAutofocus } from '@web/core/utils/hooks';
import { Component, onWillUnmount, useRef, useState } from '@odoo/owl';
import { usePos } from '@point_of_sale/app/store/pos_hook';
import { getProductQty } from '../../../utils';
export class StockListScreen extends Component {
  static template = 'wise_pos_stock.StockListScreen';
  setup() {
    this.pos = usePos();
    this.ui = useState(useService('ui'));
  }

  // Lifecycle hooks
  back() {
    this.pos.showScreen(this.pos.previousScreen);
  }

  get products() {
    const lowStockProduct = [];
    const lowStockThreshold = this.pos.config.low_stock_threshold;
    var products = this.pos.models["product.product"].getAll();
    for (let key in products) {
      const qty = getProductQty(
        this.pos.config.stock_type,
        products[key]
      );
      if (qty <= lowStockThreshold) {
        lowStockProduct.push(products[key]);
      }
    }
    return lowStockProduct;
  }
}

registry.category('pos_screens').add('StockListScreen', StockListScreen);

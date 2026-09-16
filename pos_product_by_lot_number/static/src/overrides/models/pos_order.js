/* Copyright (c) 2016-Present Webkul Software Pvt. Ltd. (<https://webkul.com/>) */
/* See LICENSE file for full copyright and licensing details. */
/* License URL : <https://store.webkul.com/license.html/> */

import { PosOrder } from "@point_of_sale/app/models/pos_order";
import { patch } from "@web/core/utils/patch";

patch(PosOrder.prototype, {

    product_total_by_lot(lot_name) {
        let count = 0;
        const lot = this.models["stock.lot"].find(lot => lot.name === lot_name);
        if (!lot) return count;
        const openOrders = this.models["pos.order"].filter(order => !order.finalized);
        return openOrders.reduce((total, order) => {
            return total + order.get_orderlines().reduce((orderTotal, orderline) => {
                if (orderline.pack_lot_ids?.some(packlot => packlot.lot_name === lot_name && lot.product_id.id === orderline.product_id.id)) {
                    return orderTotal + orderline.qty;
                }
                return orderTotal;
            }, 0);
        }, 0);
    },
    
    get_remaining_products(lot_name) {
        const lot = this.models["stock.lot"].find(lot=>lot.name===lot_name);
        if (lot) {
            var remaining_qty = lot.product_qty - this.product_total_by_lot(lot_name);
        }
        return remaining_qty
    }
});
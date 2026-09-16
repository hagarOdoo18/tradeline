/* Copyright (c) 2016-Present Webkul Software Pvt. Ltd. (<https://webkul.com/>) */
/* See LICENSE file for full copyright and licensing details. */
/* License URL : <https://store.webkul.com/license.html/> */
import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";
import { PaymentScreen } from "@point_of_sale/app/screens/payment_screen/payment_screen";

patch(PaymentScreen.prototype, {
    async validateOrder(isForceValidate) {
        var res = await super.validateOrder(...arguments);
        this.update_lot_by_orderline();
        return res;
    },

    update_lot_by_orderline() {
        for (const orderline of this.currentOrder.get_orderlines()){
            if(orderline.pack_lot_ids){
                for (const orderline_lot of orderline.pack_lot_ids){
                    const lot = this.pos.models["stock.lot"].find(lot=>lot.name===orderline_lot.lot_name);
                    if (lot && orderline.product_id.tracking == 'lot') 
                        lot.product_qty -= orderline.qty;
                    else if (lot && orderline.product_id.tracking == 'serial')
                        lot.product_qty -= 1;
                };
            }
        }
    }
});

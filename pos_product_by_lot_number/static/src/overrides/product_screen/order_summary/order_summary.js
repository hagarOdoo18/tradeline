/* Copyright (c) 2016-Present Webkul Software Pvt. Ltd. (<https://webkul.com/>) */
/* See LICENSE file for full copyright and licensing details. */
/* License URL : <https://store.webkul.com/license.html/> */

import { _t } from "@web/core/l10n/translation";
import { OrderSummary } from "@point_of_sale/app/screens/product_screen/order_summary/order_summary";
import { patch } from "@web/core/utils/patch";
import { AlertDialog } from "@web/core/confirmation_dialog/confirmation_dialog";

patch(OrderSummary.prototype, {
    
    _setValue(val) {
        const { numpadMode } = this.pos;
        let selectedLine = this.currentOrder.get_selected_orderline();
        if (selectedLine) {
            if (numpadMode === "quantity") {
                if(selectedLine.pack_lot_ids.length){
                    var lot_name = selectedLine.pack_lot_ids[0]["lot_name"];
                    var is_lot = this.pos.models["stock.lot"].find(l=>l.name == lot_name);
                    var count = this.currentOrder.product_total_by_lot(lot_name) + parseInt(val) - selectedLine.qty;
                    if (is_lot && (val > is_lot.product_qty || count > is_lot.product_qty)) {
                        var value = this.currentOrder.get_remaining_products(lot_name);
                        this.env.services.dialog.add(AlertDialog, {
                            title: _t("Out Of Quantity !"),
                            body: _t(
                                `Maximum products available to add in Lot/Serial Number ${lot_name} are ${value}.`
                            ),
                        });
                        this.numberBuffer.reset();
                    } else {
                        super._setValue(val);
                    }
                }
                else{
                    super._setValue(...arguments)
                }
            }else{
                super._setValue(...arguments)
            }
        }
    }
})
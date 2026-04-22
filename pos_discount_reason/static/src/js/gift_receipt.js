/** @odoo-module */

import { PosStore } from "@point_of_sale/app/store/pos_store";
import { patch } from "@web/core/utils/patch";

patch(PosStore.prototype, {
    async printReceipt(options = {}) {
        const order = options.order || this.get_order();
        const forceBasicReceipt = Boolean(order?.as_gift);
        return super.printReceipt({
            ...options,
            order,
            basic: Boolean(options.basic) || forceBasicReceipt,
        });
    },
});


/** @odoo-module */

import { PaymentScreen } from "@point_of_sale/app/screens/payment_screen/payment_screen";
import { patch } from "@web/core/utils/patch";

patch(PaymentScreen.prototype, {
    async _finalizeValidation() {
        const order = this.currentOrder;
        const config = this.pos.config;

        // Set to_invoice to true if auto_invoice is enabled
        if (config.auto_invoice && !order.is_to_invoice()) {
            order.set_to_invoice(true);
        }

        const result = await super._finalizeValidation(...arguments);

        // Auto print invoice if enabled and order was invoiced
        if (config.auto_print_invoice && order.is_to_invoice() && result) {
            try {
                // Trigger invoice printing
                await this._printInvoice(order);
            } catch (error) {
                console.error("Failed to auto print invoice:", error);
                // Don't block the validation process, just log the error
            }
        }

        return result;
    },

    async _printInvoice(order) {
        // This will be handled by the backend after invoice creation
        // We just need to ensure the print flag is set
        if (order.account_move) {
            // If we have the invoice already, we can print it directly
            await this.printer.printInvoice(order.account_move);
        }
    }
});
//
///** @odoo-module **/
//
//import { patch } from "@web/core/utils/patch";
//import { ProductScreen } from "@point_of_sale/app/screens/product_screen/product_screen";
//import { AlertDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
//
//patch(ProductScreen.prototype, {
//    async _onClickPay() {
//        const order = this.pos.get_order();
//
//        // discount_reason_id stored as [id, name]
//        const discountReason = order.discount_reason_id;
//
//        if (!discountReason || !discountReason[0]) {
//            this.dialog.add(AlertDialog, {
//                title: "Discount Reason Required",
//                body: "You must select a discount reason before proceeding to payment.",
//            });
//            return;
//        }
//
//        await super._onClickPay(...arguments);
//    },
//});

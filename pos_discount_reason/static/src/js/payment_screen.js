/** @odoo-module */

import { PaymentScreen } from "@point_of_sale/app/screens/payment_screen/payment_screen";
import { patch } from "@web/core/utils/patch";
import { AlertDialog, ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";

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
    },

     async _isOrderValid(isForceValidate) {

        const currentPartner = this.currentOrder.get_partner();
        const total =  this.currentOrder.get_total_with_tax();
        if ( !currentPartner.vat && currentPartner.company_type  == 'person' && total >= 15000 ) {

            this.dialog.add(AlertDialog, {
                title: ("Missing Field"),
                body: ("An Identification Number Is Required"),
            });
             this.pos.editPartner(currentPartner);
            return false;
        }
        if ( !currentPartner.vat && currentPartner.company_type  == 'company' ) {

            this.dialog.add(AlertDialog, {
                title: ("Missing Field"),
                body: ("An Vat Number Is Required"),
            });
             this.pos.editPartner(currentPartner);
            return false;
        }
        if (
            !(currentPartner.name && currentPartner.street && currentPartner.city && currentPartner.country_id && currentPartner.state_id)
        ) {
            this.dialog.add(AlertDialog, {
                title: ("Incorrect address for shipping"),
                body: ("The selected customer needs an address."),
            });
            return false;
        }
                return true;

    },
});


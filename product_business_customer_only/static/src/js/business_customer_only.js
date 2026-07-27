/** @odoo-module **/

import { PosStore } from "@point_of_sale/app/store/pos_store";
import { PosOrder } from "@point_of_sale/app/models/pos_order";
import { PaymentScreen } from "@point_of_sale/app/screens/payment_screen/payment_screen";
import { AlertDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";

function isBusinessCustomer(partner) {
    if (!partner) {
        return false;
    }
    return partner.company_type === "company" || partner.is_company === true;
}

function productIsBusinessOnly(product) {
    return Boolean(product?.business_customer_only);
}

function getBusinessOnlyLines(order) {
    return order
        .get_orderlines()
        .filter((line) => productIsBusinessOnly(line.product_id) && Number(line.qty || 0) > 0);
}

function showBusinessOnlyDialog(dialog, productNames) {
    let body = _t("This product can only be sold to business customers.");
    if (productNames?.length) {
        body = `${_t("These products can only be sold to business customers:")}\n${productNames.join("\n")}`;
    }
    dialog.add(AlertDialog, {
        title: _t("Business Customer Required"),
        body,
    });
}

function canSellBusinessOnlyProduct(pos, dialog, product) {
    if (!productIsBusinessOnly(product)) {
        return true;
    }
    const order = pos.get_order();
    if (isBusinessCustomer(order?.get_partner())) {
        return true;
    }
    showBusinessOnlyDialog(dialog, [product.display_name]);
    return false;
}

patch(PosStore.prototype, {
    async addLineToCurrentOrder(vals, options, configure) {
        const product = vals?.product_id;
        if (!canSellBusinessOnlyProduct(this, this.dialog, product)) {
            return;
        }
        return await super.addLineToCurrentOrder(...arguments);
    },
});

patch(PosOrder.prototype, {
    add_product(product, options) {
        const pos = this.pos || this.env?.services?.pos;
        const dialog = pos?.dialog || this.env?.services?.dialog;
        if (pos && dialog && !canSellBusinessOnlyProduct(pos, dialog, product)) {
            return;
        }
        if (typeof super.add_product === "function") {
            return super.add_product(...arguments);
        }
        return super.addProduct(...arguments);
    },

    addProduct(product, options) {
        const pos = this.pos || this.env?.services?.pos;
        const dialog = pos?.dialog || this.env?.services?.dialog;
        if (pos && dialog && !canSellBusinessOnlyProduct(pos, dialog, product)) {
            return;
        }
        if (typeof super.addProduct === "function") {
            return super.addProduct(...arguments);
        }
        return super.add_product(...arguments);
    },
});

patch(PaymentScreen.prototype, {
    async _isOrderValid(isForceValidate) {
        const order = this.currentOrder;
        const restrictedLines = getBusinessOnlyLines(order);
        if (restrictedLines.length && !isBusinessCustomer(order.get_partner())) {
            showBusinessOnlyDialog(
                this.dialog,
                restrictedLines.map((line) => `- ${line.product_id.display_name}`)
            );
            return false;
        }
        return await super._isOrderValid(...arguments);
    },
});

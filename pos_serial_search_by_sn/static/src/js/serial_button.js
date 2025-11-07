/** @odoo-module **/

import { ProductScreen } from "@point_of_sale/app/screens/product_screen/product_screen";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component } from "@odoo/owl";

/**
 * A control button on the product screen that prompts for a Serial Number
 * and adds the corresponding product to the current order.
 */
export class SerialSearchButton extends Component {
    setup() {
        this.popup = useService("popup");
        this.pos = this.env.pos;
    }

    async onClick() {
        const { confirmed, payload } = await this.popup.add("TextPopup", {
            title: this.env._t("Search by Serial Number"),
            startingValue: "",
            placeholder: this.env._t("Enter Serial Number..."),
        });
        if (!confirmed) return;
        const sn = (payload || "").trim();
        if (!sn) return;

        if (!this.pos.serialIndex || !this.pos.serialIndex[sn]) {
            await this.popup.add("ConfirmPopup", {
                title: this.env._t("Not Found"),
                body: this.env._t("No product found for this Serial Number."),
            });
            return;
        }
        const productId = this.pos.serialIndex[sn];
        const product = this.pos.db.get_product_by_id(productId);
        if (!product) {
            await this.popup.add("ConfirmPopup", {
                title: this.env._t("Product Missing"),
                body: this.env._t("Product exists for SN but is not loaded in POS."),
            });
            return;
        }
        this.pos.get_order().add_product(product, { description: `SN ${sn}` });
    }
}
SerialSearchButton.template = "pos_serial_search_by_sn.SerialSearchButton";

// Add button to Product Screen control buttons row
registry.category("pos_product_screen_control_buttons").add("serial_search_button", {
    component: SerialSearchButton,
    condition: () => true,
});

/** @odoo-module **/

import { ProductScreen } from "@point_of_sale/app/screens/product_screen/product_screen";
import { patch } from "@web/core/utils/patch";

console.log("serial-search: module loaded ✅");

// helper to detect POS environment
function getPosEnv() {
    if (window.pos) return window.pos;
    if (window.posmodel) return window.posmodel;
    if (window.posModel) return window.posModel;
    return null;
}

patch(ProductScreen.prototype, {
    setup() {
        super.setup();

        // attach only once
        setTimeout(() => {
            const input = document.querySelector("input[placeholder='Search products...']");
            if (input && !input.dataset.serialListener) {
                input.dataset.serialListener = "1";

                input.addEventListener("keyup", async (ev) => {
                    if (ev.key === "Enter" && input.value) {
                        const serial = input.value.trim();
                        console.log("serial-search: user entered", serial);

                        try {
                            // ✅ use orm service from env
                            const lots = await this.env.services.orm.searchRead(
                                "stock.lot",
                                [["name", "=", serial]],
                                ["id", "name", "product_id"],
                                { limit: 1 }
                            );

                            if (!lots.length) {
                                console.warn("serial-search: no lot found");
                                return;
                            }

                            const lot = lots[0];
                            const product_id = lot.product_id?.[0];
                            if (!product_id) {
                                console.warn("serial-search: lot has no product");
                                return;
                            }

                            const pos = this.env.services.pos;
                            const order = pos.get_order();
                            if (!pos) {
                                console.error("serial-search: no POS env found");
                                return;
                            }
                            if (!order) {
                            console.warn("serial-search: no active order found");
                            return;
                        }




                            let product = null;
                            if (pos.db && pos.db.get_product_by_id) {
                                product = pos.db.get_product_by_id(product_id);
                            } else if (pos.models && pos.models["product.product"]) {
                                product = pos.models["product.product"].get(product_id);
                            }
                            if (!product) {
                                console.warn("serial-search: no product found to add");
                                return;
                            }
                            if (!order) {
                            console.warn("DEBUG: No active order found");
                        } else {
                            console.log("DEBUG: order typeof:", typeof order);
                            console.log("DEBUG: order constructor:", order.constructor?.name);
                            console.log("DEBUG: order full object:", order);
                        }


                            if (product) {
                             console.log("DEBUG: order full PRODUCT:", product.id);
                             console.log("DEBUG: order full order:", order);
                             order.add_product(product, {
                                quantity: 1,
                                // Optionally, you can pass the serial number as an 'extra' here
                                // if you have extended the pos.order.line model to store it.
                                // extras: { serial_number: serial }
                            });



                                input.value = "";
                                console.log("serial-search: added product", product.display_name);
                            } else {
                                console.warn("serial-search: product not found in POS cache", product.display_name);
                            }
                        } catch (err) {
                            console.error("serial-search: RPC error", err);
                        }
                    }
                });

                console.log("serial-search: listener attached to search box ✅");
            }
        }, 1500);
    },
});

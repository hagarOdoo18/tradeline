/** @odoo-module */

import { ControlButtons } from "@point_of_sale/app/screens/product_screen/control_buttons/control_buttons";
import { patch } from "@web/core/utils/patch";
import { _t } from "@web/core/l10n/translation";
import { SelectionPopup } from "@point_of_sale/app/utils/input_popups/selection_popup";
import { makeAwaitable } from "@point_of_sale/app/store/make_awaitable_dialog";
import { AlertDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { rpc } from "@web/core/network/rpc";

function asNumber(value, fallback = 0) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
}

function getModelRecord(pos, modelName, id) {
    if (!pos?.models?.[modelName] || !id) {
        return null;
    }
    return pos.models[modelName].getBy("id", id) || null;
}

function buildQuotationLabel(quotation) {
    const partnerName = quotation.partner_name || _t("Walk-in Customer");
    const amountLabel = quotation.amount_total_label || "";
    const validityLabel = quotation.validity_label || _t("No Expiration");
    return `${quotation.name} - ${partnerName} - ${amountLabel} - ${_t("Exp")}: ${validityLabel}`;
}

patch(ControlButtons.prototype, {
    async selectDownpaymentQuotation() {
        const order = this.pos.get_order();
        if (!order) {
            this.dialog.add(AlertDialog, {
                title: _t("No Active Order"),
                body: _t("Please create or select an order first."),
            });
            return;
        }

        if (order.get_orderlines().length) {
            this.dialog.add(AlertDialog, {
                title: _t("Order Not Empty"),
                body: _t("Please use a new empty order before loading a downpayment quotation."),
            });
            return;
        }

        try {
            const quotations = await rpc("/web/dataset/call_kw/pos.order/get_valid_downpayment_quotations_pos", {
                model: "pos.order",
                method: "get_valid_downpayment_quotations_pos",
                args: [false, 120],
                kwargs: {},
            });

            if (!Array.isArray(quotations) || !quotations.length) {
                this.dialog.add(AlertDialog, {
                    title: _t("No Valid Quotations"),
                    body: _t("No valid downpayment quotations are currently available."),
                });
                return;
            }

            const selectionList = quotations.map((quotation) => ({
                id: quotation.id,
                item: quotation,
                label: buildQuotationLabel(quotation),
                isSelected: false,
            }));

            const selected = await makeAwaitable(this.dialog, SelectionPopup, {
                title: _t("Select Downpayment Quotation"),
                list: selectionList,
            });
            if (!selected || !selected.id) {
                return;
            }

            const details = await rpc("/web/dataset/call_kw/pos.order/get_downpayment_quotation_details_pos", {
                model: "pos.order",
                method: "get_downpayment_quotation_details_pos",
                args: [selected.id],
                kwargs: {},
            });

            if (!details || !Array.isArray(details.lines) || !details.lines.length) {
                this.dialog.add(AlertDialog, {
                    title: _t("No Usable Lines"),
                    body: _t("This quotation has no downpayment lines available in POS."),
                });
                return;
            }

            if (details.partner_id) {
                const partner = getModelRecord(this.pos, "res.partner", details.partner_id);
                if (partner) {
                    order.set_partner(partner);
                }
            }

            const missingProducts = [];
            for (const line of details.lines) {
                const product = getModelRecord(this.pos, "product.product", line.product_id);
                if (!product) {
                    missingProducts.push(line.product_name || `#${line.product_id}`);
                    continue;
                }

                const quantity = Math.max(asNumber(line.qty, 1), 1);
                order.add_product(product, { quantity, merge: false });
                const selectedLine = order.get_selected_orderline();
                if (!selectedLine) {
                    continue;
                }
                selectedLine.set_quantity(quantity);
                selectedLine.set_unit_price(asNumber(line.price_unit, selectedLine.get_unit_price()));
                selectedLine.set_discount(Math.max(0, asNumber(line.discount, 0)));
            }

            order.downpayment_quotation_id = details.quotation_id || false;
            order.downpayment_quotation_name = details.quotation_name || "";

            const backendMissing = Array.isArray(details.missing_products) ? details.missing_products : [];
            const allMissing = [...new Set([...backendMissing, ...missingProducts])];
            if (allMissing.length) {
                this.dialog.add(AlertDialog, {
                    title: _t("Some Products Not In POS"),
                    body: _t("The following quotation products are not available in POS and were skipped: ") + allMissing.join(", "),
                });
            }
        } catch (error) {
            this.dialog.add(AlertDialog, {
                title: _t("Loading Failed"),
                body: _t("Could not load downpayment quotations. ") + (error?.message || ""),
            });
        }
    },
});

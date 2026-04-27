/** @odoo-module */

import { ControlButtons } from "@point_of_sale/app/screens/product_screen/control_buttons/control_buttons";
import { patch } from "@web/core/utils/patch";
import { _t } from "@web/core/l10n/translation";
import { SelectionPopup } from "@point_of_sale/app/utils/input_popups/selection_popup";
import { TextInputPopup } from "@point_of_sale/app/utils/input_popups/text_input_popup";
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

function getSourceTypeLabel(source) {
    if (source?.inv_type === "invoice") {
        return _t("Invoice");
    }
    if (source?.inv_type === "quotation") {
        return _t("Quotation");
    }
    return _t("Source");
}

function buildSourceLabel(source) {
    const sourceType = getSourceTypeLabel(source);
    const reference = source.reference_number ? ` [${source.reference_number}]` : "";
    const partnerName = source.partner_name || _t("Walk-in Customer");
    const amountLabel = source.amount_total_label || "";
    const validityLabel = source.validity_label || _t("No Expiration");
    return `${sourceType}${reference} ${source.name} - ${partnerName} - ${amountLabel} - ${_t("Exp")}: ${validityLabel}`;
}

function getSelectionId(selection) {
    if (selection === null || selection === undefined) {
        return false;
    }
    if (typeof selection === "string" || typeof selection === "number") {
        return selection;
    }
    if (selection.id !== undefined && selection.id !== null) {
        return selection.id;
    }
    if (selection.item && selection.item.id !== undefined && selection.item.id !== null) {
        return selection.item.id;
    }
    if (selection.payload && selection.payload.id !== undefined && selection.payload.id !== null) {
        return selection.payload.id;
    }
    return false;
}

function getTextInputValue(result) {
    if (typeof result === "string") {
        return result.trim();
    }
    if (result && typeof result === "object") {
        if (typeof result.payload === "string") {
            return result.payload.trim();
        }
        if (typeof result.text === "string") {
            return result.text.trim();
        }
        if (typeof result.value === "string") {
            return result.value.trim();
        }
    }
    return "";
}

function getCurrentPosConfigId(pos) {
    const raw = pos?.config?.id;
    if (raw === null || raw === undefined) {
        return false;
    }
    if (typeof raw === "number") {
        return raw;
    }
    if (Array.isArray(raw) && raw.length) {
        return asNumber(raw[0], 0) || false;
    }
    if (typeof raw === "object" && raw.id !== undefined) {
        return asNumber(raw.id, 0) || false;
    }
    return asNumber(raw, 0) || false;
}

function getCurrentPosBranchId(pos) {
    const raw = pos?.config?.branch_id;
    if (raw === null || raw === undefined) {
        return false;
    }
    if (typeof raw === "number") {
        return raw;
    }
    if (Array.isArray(raw) && raw.length) {
        return asNumber(raw[0], 0) || false;
    }
    if (typeof raw === "object" && raw.id !== undefined) {
        return asNumber(raw.id, 0) || false;
    }
    return asNumber(raw, 0) || false;
}

function addProductToOrderCompat(pos, order, product, quantity) {
    const options = { quantity, merge: false };
    if (order && typeof order.add_product === "function") {
        order.add_product(product, options);
        return order;
    }
    if (order && typeof order.addProduct === "function") {
        order.addProduct(product, options);
        return order;
    }
    if (pos && typeof pos.addProductToCurrentOrder === "function") {
        pos.addProductToCurrentOrder(product, options);
        return pos.get_order ? pos.get_order() : order;
    }
    throw new Error(_t("Could not add product to order in this POS runtime."));
}

function getSelectedOrderlineCompat(order) {
    if (!order) {
        return null;
    }
    if (typeof order.get_selected_orderline === "function") {
        return order.get_selected_orderline();
    }
    if (typeof order.getSelectedOrderline === "function") {
        return order.getSelectedOrderline();
    }
    return null;
}

function setLineValuesCompat(line, quantity, priceUnit, discount) {
    if (!line) {
        return;
    }
    if (typeof line.set_quantity === "function") {
        line.set_quantity(quantity);
    } else if (typeof line.setQuantity === "function") {
        line.setQuantity(quantity);
    }

    if (typeof line.set_unit_price === "function") {
        line.set_unit_price(priceUnit);
    } else if (typeof line.setUnitPrice === "function") {
        line.setUnitPrice(priceUnit);
    }

    if (typeof line.set_discount === "function") {
        line.set_discount(discount);
    } else if (typeof line.setDiscount === "function") {
        line.setDiscount(discount);
    }
}

patch(ControlButtons.prototype, {
    async _loadDownpaymentSourceIntoOrder(order, details) {
        if (!details) {
            this.dialog.add(AlertDialog, {
                title: _t("No Source Data"),
                body: _t("Could not read data from selected source."),
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
        let workingOrder = order;
        for (const line of details.lines) {
            const product = getModelRecord(this.pos, "product.product", line.product_id);
            if (!product) {
                missingProducts.push(line.product_name || `#${line.product_id}`);
                continue;
            }

            const quantity = Math.max(asNumber(line.qty, 1), 1);
            workingOrder = addProductToOrderCompat(this.pos, workingOrder, product, quantity);
            const selectedLine = getSelectedOrderlineCompat(workingOrder);
            if (!selectedLine) {
                continue;
            }
            const currentUnitPrice = typeof selectedLine.get_unit_price === "function"
                ? selectedLine.get_unit_price()
                : (typeof selectedLine.getUnitPrice === "function" ? selectedLine.getUnitPrice() : 0);
            setLineValuesCompat(
                selectedLine,
                quantity,
                asNumber(line.price_unit, currentUnitPrice),
                Math.max(0, asNumber(line.discount, 0))
            );
        }

        const orderToUpdate = workingOrder || order;
        orderToUpdate.downpayment_quotation_id = details.source_id || details.quotation_id || false;
        orderToUpdate.downpayment_quotation_name = details.source_name || details.quotation_name || "";

        if (!Array.isArray(details.lines) || !details.lines.length) {
            this.dialog.add(AlertDialog, {
                title: _t("Customer Loaded"),
                body: _t("Customer was loaded from source, but no usable downpayment lines were found."),
            });
            return;
        }

        const backendMissing = Array.isArray(details.missing_products) ? details.missing_products : [];
        const allMissing = [...new Set([...backendMissing, ...missingProducts])];
        if (allMissing.length) {
            this.dialog.add(AlertDialog, {
                title: _t("Some Products Not In POS"),
                body: _t("The following downpayment products are not available in POS and were skipped: ") + allMissing.join(", "),
            });
        }
    },

    async _openManualDownpaymentLookup(order) {
        const popupResult = await makeAwaitable(this.dialog, TextInputPopup, {
            title: _t("Manual Downpayment Reference"),
            placeholder: _t("Enter reference number..."),
        });
        const manualReference = getTextInputValue(popupResult);
        if (!manualReference) {
            return;
        }
        order.downpayment_quotation_id = false;
        order.downpayment_quotation_name = `${_t("Manual Ref")}: ${manualReference}`;
        this.dialog.add(AlertDialog, {
            title: _t("Manual Mode"),
            body: _t("Reference saved. Please enter customer and other fields manually."),
        });
    },

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
                body: _t("Please use a new empty order before loading a downpayment source."),
            });
            return;
        }

        try {
            const currentConfigId = getCurrentPosConfigId(this.pos);
            const currentBranchId = getCurrentPosBranchId(this.pos);
            const sources = await rpc("/web/dataset/call_kw/pos.order/get_valid_downpayment_quotations_pos", {
                model: "pos.order",
                method: "get_valid_downpayment_quotations_pos",
                args: [false, 120, false, currentBranchId, currentConfigId],
                kwargs: {},
            });

            const selectionList = [{
                id: "__manual_reference__",
                item: { id: "__manual_reference__" },
                label: _t("Manual Reference"),
                isSelected: false,
            }];

            if (Array.isArray(sources) && sources.length) {
                selectionList.push(...sources.map((source) => ({
                    id: source.id,
                    item: source,
                    label: buildSourceLabel(source),
                    isSelected: false,
                })));
            }

            const selected = await makeAwaitable(this.dialog, SelectionPopup, {
                title: _t("Select Downpayment Source"),
                list: selectionList,
            });
            const selectedId = getSelectionId(selected);
            if (!selectedId) {
                return;
            }

            if (selectedId === "__manual_reference__") {
                await this._openManualDownpaymentLookup(order);
                return;
            }

            const details = await rpc("/web/dataset/call_kw/pos.order/get_downpayment_quotation_details_pos", {
                model: "pos.order",
                method: "get_downpayment_quotation_details_pos",
                args: [asNumber(selectedId, 0), false, false, true, currentBranchId, currentConfigId],
                kwargs: {},
            });

            await this._loadDownpaymentSourceIntoOrder(order, details);
        } catch (error) {
            const backendMessage =
                error?.data?.message ||
                error?.message ||
                _t("Unexpected error while loading downpayment source.");
            this.dialog.add(AlertDialog, {
                title: _t("Loading Failed"),
                body: backendMessage,
            });
        }
    },
});

/** @odoo-module */

import { Component, onWillStart, useState } from "@odoo/owl";
import { ControlButtons } from "@point_of_sale/app/screens/product_screen/control_buttons/control_buttons";
import { PosOrder } from "@point_of_sale/app/models/pos_order";
import { Dialog } from "@web/core/dialog/dialog";
import { ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { Input } from "@point_of_sale/app/generic_components/inputs/input/input";
import { _t } from "@web/core/l10n/translation";
import { rpc } from "@web/core/network/rpc";
import { patch } from "@web/core/utils/patch";
import { useService } from "@web/core/utils/hooks";


function rpcErrorMessage(error) {
    return error?.data?.message || error?.message || _t("The server could not complete the request.");
}

function posConfigId(pos) {
    const raw = pos?.config?.id;
    if (Array.isArray(raw)) {
        return raw[0] || false;
    }
    return raw?.id || raw || false;
}

function newRequestToken() {
    if (globalThis.crypto?.randomUUID) {
        return globalThis.crypto.randomUUID();
    }
    return `preorder-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function modelRecord(pos, modelName, id) {
    return pos?.models?.[modelName]?.getBy?.("id", id) || null;
}

function addProductToOrder(pos, order, product, quantity) {
    const options = { quantity, merge: false };
    if (order && typeof order.addProduct === "function") {
        order.addProduct(product, options);
        return order;
    }
    if (pos && typeof pos.addProductToCurrentOrder === "function") {
        pos.addProductToCurrentOrder(product, options);
        return pos.get_order ? pos.get_order() : order;
    }
    if (order && typeof order.add_product === "function") {
        order.add_product(product, options);
        return order;
    }
    throw new Error(_t("Could not add the pre-order product to the current POS order."));
}

function selectedOrderline(order) {
    if (typeof order?.getSelectedOrderline === "function") {
        return order.getSelectedOrderline();
    }
    if (typeof order?.get_selected_orderline === "function") {
        return order.get_selected_orderline();
    }
    return null;
}

function setOrderlineValues(line, quantity, priceUnit, discount) {
    if (!line) {
        return;
    }
    if (typeof line.setQuantity === "function") {
        line.setQuantity(quantity);
    } else if (typeof line.set_quantity === "function") {
        line.set_quantity(quantity);
    }
    if (typeof line.setUnitPrice === "function") {
        line.setUnitPrice(priceUnit);
    } else if (typeof line.set_unit_price === "function") {
        line.set_unit_price(priceUnit);
    }
    if (typeof line.setDiscount === "function") {
        line.setDiscount(discount);
    } else if (typeof line.set_discount === "function") {
        line.set_discount(discount);
    }
}


export class PreorderDeliveryDialog extends Component {
    static template = "preorder_management.PreorderDeliveryDialog";
    static components = { Dialog, Input };
    static props = {
        pos: Object,
        close: Function,
    };

    setup() {
        this.dialog = useService("dialog");
        this.ui = useState(useService("ui"));
        this.state = useState({
            loading: true,
            processing: false,
            query: "",
            preorders: [],
            selected: null,
            serials: {},
            paymentLines: [],
            error: "",
            success: null,
        });
        this.requestToken = null;
        onWillStart(() => this.loadPreorders());
    }

    get configId() {
        return posConfigId(this.props.pos);
    }

    get title() {
        return _t("Pre-order Delivery");
    }

    get searchPlaceholder() {
        return _t("Search pre-order, customer, phone, or device...");
    }

    get filteredPreorders() {
        const query = (this.state.query || "").trim().toLowerCase();
        if (!query) {
            return this.state.preorders;
        }
        return this.state.preorders.filter((preorder) =>
            [preorder.name, preorder.customer_name, preorder.customer_phone, preorder.device_summary]
                .filter(Boolean)
                .some((value) => String(value).toLowerCase().includes(query))
        );
    }

    get serialAssignments() {
        const result = {};
        for (const line of this.state.selected?.lines || []) {
            if (line.tracking !== "serial") {
                continue;
            }
            result[String(line.product_id)] = (this.state.serials[line.product_id] || [])
                .map((value) => String(value || "").trim());
        }
        return result;
    }

    get canFinalize() {
        if (!this.state.selected || this.state.processing) {
            return false;
        }
        const serialsReady = (this.state.selected.lines || []).every((line) => {
            if (line.tracking !== "serial") {
                return true;
            }
            const serials = this.state.serials[line.product_id] || [];
            return serials.length === Math.round(line.qty) && serials.every((value) => String(value || "").trim());
        });
        if (!serialsReady) {
            return false;
        }
        if (this.state.selected.payment_recording_mode !== "delivery") {
            return true;
        }
        const total = this.state.paymentLines.reduce(
            (sum, line) => sum + (Number.parseFloat(line.amount) || 0),
            0
        );
        return this.state.paymentLines.length > 0 &&
            Math.abs(total - Number(this.state.selected.amount || 0)) < 0.005 &&
            this.state.paymentLines.every((line) => line.method_id && Number.parseFloat(line.amount) > 0);
    }

    serialSlots(line) {
        return Array.from({ length: Math.max(0, Math.round(line.qty)) }, (_value, index) => index);
    }

    serialValue(productId, index) {
        return this.state.serials[productId]?.[index] || "";
    }

    updateSerial(productId, index, event) {
        const values = [...(this.state.serials[productId] || [])];
        values[index] = event.target.value;
        this.state.serials = { ...this.state.serials, [productId]: values };
        this.state.error = "";
    }

    updatePaymentMethod(index, event) {
        const lines = this.state.paymentLines.map((line, lineIndex) =>
            lineIndex === index ? { ...line, method_id: Number(event.target.value) || false } : line
        );
        this.state.paymentLines = lines;
        this.state.error = "";
    }

    updatePaymentAmount(index, event) {
        const lines = this.state.paymentLines.map((line, lineIndex) =>
            lineIndex === index ? { ...line, amount: event.target.value } : line
        );
        this.state.paymentLines = lines;
        this.state.error = "";
    }

    addPaymentLine() {
        const method = this.state.selected?.payment_methods?.[0];
        this.state.paymentLines = [
            ...this.state.paymentLines,
            { method_id: method?.id || false, amount: 0 },
        ];
    }

    removePaymentLine(index) {
        this.state.paymentLines = this.state.paymentLines.filter((_line, lineIndex) => lineIndex !== index);
    }

    async loadPreorders() {
        this.state.loading = true;
        this.state.error = "";
        try {
            const records = await rpc(
                "/web/dataset/call_kw/sale.preorder/get_ready_preorders_pos",
                {
                    model: "sale.preorder",
                    method: "get_ready_preorders_pos",
                    args: [this.configId, false, 120],
                    kwargs: {},
                }
            );
            this.state.preorders = Array.isArray(records) ? records : [];
            if (this.state.selected) {
                const stillReady = this.state.preorders.some((item) => item.id === this.state.selected.id);
                if (!stillReady) {
                    this.state.selected = null;
                }
            }
        } catch (error) {
            this.state.error = rpcErrorMessage(error);
        } finally {
            this.state.loading = false;
        }
    }

    async selectPreorder(preorder) {
        if (this.state.processing) {
            return;
        }
        this.state.loading = true;
        this.state.error = "";
        try {
            const details = await rpc(
                "/web/dataset/call_kw/sale.preorder/get_preorder_delivery_details_pos",
                {
                    model: "sale.preorder",
                    method: "get_preorder_delivery_details_pos",
                    args: [preorder.id, this.configId],
                    kwargs: {},
                }
            );
            this.state.selected = details;
            this.state.success = null;
            this.requestToken = null;
            const serials = {};
            for (const line of details.lines || []) {
                if (line.tracking === "serial") {
                    serials[line.product_id] = Array(Math.max(0, Math.round(line.qty))).fill("");
                }
            }
            this.state.serials = serials;
            this.state.paymentLines = [];
            // Migrated pre-orders are fulfilled through the normal POS checkout.
            // Selecting one is the deliberate user action that loads it into the cart;
            // legacy pre-orders still require the detail/serial/payment confirmation flow.
            if (details.payment_recording_mode === "delivery") {
                await this.addToCart();
            }
        } catch (error) {
            this.state.error = rpcErrorMessage(error);
        } finally {
            this.state.loading = false;
        }
    }

    async addToCart() {
        const details = this.state.selected;
        if (!details || this.state.processing) {
            return;
        }
        const order = this.props.pos?.get_order?.();
        if (!order) {
            this.state.error = _t("Open a POS order before adding a pre-order.");
            return;
        }
        if (order.preorder_id && Number(order.preorder_id) !== Number(details.id)) {
            this.state.error = _t("This cart already contains another pre-order. Complete it before loading a different one.");
            return;
        }

        const currentPartner = order.get_partner?.() || order.getPartner?.();
        if (currentPartner && currentPartner.id !== details.customer_id && order.get_orderlines?.().length) {
            this.state.error = _t("The current cart belongs to another customer. Start a new order for this pre-order.");
            return;
        }

        const products = (details.lines || []).map((line) => ({
            line,
            product: modelRecord(this.props.pos, "product.product", line.product_id),
        }));
        const missing = products.filter((item) => !item.product);
        if (missing.length) {
            this.state.error = _t("These pre-order products are not loaded in this POS: %s")
                .replace("%s", missing.map((item) => item.line.product_name || item.line.product_id).join(", "));
            return;
        }

        this.state.processing = true;
        this.state.error = "";
        try {
            const partner = modelRecord(this.props.pos, "res.partner", details.customer_id);
            if (!partner) {
                throw new Error(_t("The pre-order customer is not available in this POS."));
            }
            if (typeof order.set_partner === "function") {
                order.set_partner(partner);
            } else if (typeof order.setPartner === "function") {
                order.setPartner(partner);
            }

            let workingOrder = order;
            for (const { line, product } of products) {
                const quantity = Number(line.qty || 0);
                workingOrder = addProductToOrder(this.props.pos, workingOrder, product, quantity);
                setOrderlineValues(
                    selectedOrderline(workingOrder),
                    quantity,
                    Number(line.price_unit || 0),
                    Number(line.discount || 0)
                );
            }
            workingOrder.preorder_id = details.id;
            workingOrder.preorder_name = details.name || "";
            workingOrder.preorder_line_ids = (details.lines || []).map((line) => line.id);
            workingOrder.to_invoice = true;
            this.props.close();
        } catch (error) {
            this.state.error = error?.message || _t("The pre-order could not be added to the cart.");
        } finally {
            this.state.processing = false;
        }
    }

    requestFinalize() {
        if (!this.canFinalize) {
            this.state.error = _t("Enter every required serial number before delivery.");
            return;
        }
        const selected = this.state.selected;
        this.dialog.add(ConfirmationDialog, {
            title: _t("Deliver and Invoice Pre-order"),
            body: selected.payment_recording_mode === "delivery"
                ? _t("Confirm delivery of %s to %s and record the delivery payment.", selected.name, selected.customer_name)
                : _t("Confirm delivery of %s to %s. The original payment will be applied; the customer will not be charged again.", selected.name, selected.customer_name),
            confirmLabel: selected.payment_recording_mode === "delivery" ? _t("Pay, Deliver & Invoice") : _t("Deliver & Invoice"),
            confirm: () => this.finalize(),
        });
    }

    async finalize() {
        if (!navigator.onLine) {
            this.state.error = _t("Pre-order delivery requires an online connection.");
            return;
        }
        this.state.processing = true;
        this.state.error = "";
        this.requestToken ||= newRequestToken();
        try {
            const result = await rpc(
                "/web/dataset/call_kw/sale.preorder/finalize_preorder_delivery_pos",
                {
                    model: "sale.preorder",
                    method: "finalize_preorder_delivery_pos",
                    args: [
                        this.state.selected.id,
                        this.serialAssignments,
                        this.configId,
                        this.requestToken,
                        this.state.paymentLines,
                    ],
                    kwargs: {},
                }
            );
            this.state.success = result;
            this.state.preorders = this.state.preorders.filter(
                (preorder) => preorder.id !== this.state.selected.id
            );
        } catch (error) {
            this.state.error = rpcErrorMessage(error);
        } finally {
            this.state.processing = false;
        }
    }

    print(url) {
        if (url) {
            window.open(url, "_blank", "noopener,noreferrer");
        }
    }

    deliverAnother() {
        this.state.selected = null;
        this.state.success = null;
        this.state.serials = {};
        this.state.paymentLines = [];
        this.requestToken = null;
        this.loadPreorders();
    }
}


patch(PosOrder.prototype, {
    setup(vals) {
        super.setup(...arguments);
        this.preorder_id = this.preorder_id || vals?.preorder_id || false;
        this.preorder_name = this.preorder_name || vals?.preorder_name || "";
        this.preorder_line_ids = this.preorder_line_ids || vals?.preorder_line_ids || [];
    },

    serialize() {
        const serialized = super.serialize(...arguments);
        serialized.preorder_id = this.preorder_id || false;
        serialized.preorder_name = this.preorder_name || "";
        serialized.preorder_line_ids = Array.isArray(this.preorder_line_ids)
            ? this.preorder_line_ids
            : [];
        if (this.preorder_id) {
            // A pre-order cart must produce the normal POS customer invoice.
            serialized.to_invoice = true;
        }
        return serialized;
    },
});


patch(ControlButtons.prototype, {
    openPreorderDelivery() {
        this.dialog.add(PreorderDeliveryDialog, { pos: this.pos });
    },
});

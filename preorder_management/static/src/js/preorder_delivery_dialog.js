/** @odoo-module */

import { Component, onWillStart, useState } from "@odoo/owl";
import { ControlButtons } from "@point_of_sale/app/screens/product_screen/control_buttons/control_buttons";
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
        return (this.state.selected.lines || []).every((line) => {
            if (line.tracking !== "serial") {
                return true;
            }
            const serials = this.state.serials[line.product_id] || [];
            return serials.length === Math.round(line.qty) && serials.every((value) => String(value || "").trim());
        });
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
        } catch (error) {
            this.state.error = rpcErrorMessage(error);
        } finally {
            this.state.loading = false;
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
            body: _t(
                "Confirm delivery of %s to %s. The original payment will be applied; the customer will not be charged again.",
                selected.name,
                selected.customer_name
            ),
            confirmLabel: _t("Deliver & Invoice"),
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
        this.requestToken = null;
        this.loadPreorders();
    }
}


patch(ControlButtons.prototype, {
    openPreorderDelivery() {
        this.dialog.add(PreorderDeliveryDialog, { pos: this.pos });
    },
});

/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";
import { makeAwaitable } from "@point_of_sale/app/store/make_awaitable_dialog";
import { PosStore } from "@point_of_sale/app/store/pos_store";
import {
    BaseProductAttribute,
    ProductConfiguratorPopup,
} from "@point_of_sale/app/store/product_configurator_popup/product_configurator_popup";

function getMappedValue(mapping, key) {
    if (!mapping) {
        return false;
    }
    return mapping[key] || mapping[String(key)] || false;
}

function normalizeId(value) {
    const id = Number(value);
    return Number.isInteger(id) && id > 0 ? id : false;
}

function applyAutoVendorSelection(payload, availability) {
    if (!payload || !availability) {
        return payload;
    }

    const attributeValueIds = Array.isArray(payload.attribute_value_ids)
        ? payload.attribute_value_ids.map((value) => normalizeId(value)).filter(Boolean)
        : [];

    const vendorValueByValueId = availability.vendor_value_by_value_id || {};
    let selectedVendorId = false;

    for (const valueId of attributeValueIds) {
        const candidate = normalizeId(getMappedValue(vendorValueByValueId, valueId));
        if (candidate) {
            selectedVendorId = candidate;
            break;
        }
    }

    if (!selectedVendorId) {
        selectedVendorId = normalizeId(availability.default_vendor_value_id);
    }

    if (selectedVendorId && !attributeValueIds.includes(selectedVendorId)) {
        attributeValueIds.push(selectedVendorId);
    }

    payload.attribute_value_ids = attributeValueIds;
    return payload;
}

ProductConfiguratorPopup.props = {
    ...ProductConfiguratorPopup.props,
    availability: { type: Object, optional: true },
};

patch(PosStore.prototype, {
    async openConfigurator(product, opts = {}) {
        const tracking = product?.tracking || product?.raw?.tracking;
        const isTrackedProduct = tracking === "serial" || tracking === "lot";

        let availability = {};
        if (!isTrackedProduct && product?.raw?.product_tmpl_id && this.config?.id) {
            try {
                availability = await this.data.call(
                    "product.template",
                    "get_pos_configurator_availability",
                    [product.raw.product_tmpl_id, this.config.id, opts.qty || opts.quantity || 1]
                );
                if (!availability || typeof availability !== "object") {
                    availability = {};
                }
            } catch (error) {
                console.error("Failed to fetch POS configurator availability.", error);
                availability = {};
            }
        }

        const attrById = this.models["product.attribute"].getAllBy("id");
        let attributeLines = product.attribute_line_ids.filter((attr) => attr.attribute_id?.id in attrById);
        if (opts.code) {
            attributeLines = attributeLines.filter(
                (attr) => attr.attribute_id.create_variant === "no_variant"
            );
        }
        const attributeLinesValues = attributeLines.map((attr) => attr.product_template_value_ids);
        if (attributeLinesValues.some((values) => values.length > 1 || values[0].is_custom)) {
            let defaultValues = {};
            const match = product.barcode && product.barcode.includes(this.searchProductWord);
            if (this.searchProductWord && match) {
                defaultValues = Object.fromEntries(
                    product.product_template_variant_value_ids.map((value) => [
                        value.attribute_line_id.id,
                        value.id.toString(),
                    ])
                );
            }

            const payload = await makeAwaitable(this.dialog, ProductConfiguratorPopup, {
                product: product,
                hideAlwaysVariants: opts.hideAlwaysVariants,
                defaultValues: defaultValues,
                availability: availability,
            });

            return applyAutoVendorSelection(payload, availability);
        }

        const payload = {
            attribute_value_ids: attributeLinesValues.map((values) => values[0].id),
            attribute_custom_values: [],
            price_extra: attributeLinesValues
                .filter((attr) => attr[0].attribute_id.create_variant === "no_variant")
                .reduce((acc, values) => acc + values[0].price_extra, 0),
            quantity: 1,
        };

        return applyAutoVendorSelection(payload, availability);
    },
});

patch(BaseProductAttribute.prototype, {
    setup() {
        super.setup(...arguments);
        if (!this.values?.length || this.attributeLine.attribute_id.display_type === "multi") {
            return;
        }

        const selectedValueId = parseInt(this.state.attribute_value_ids, 10);
        const hasSelectedValue = this.values.some((value) => value.id === selectedValueId);
        if (!hasSelectedValue) {
            this.state.attribute_value_ids = this.values[0].id.toString();
        }
    },
});

patch(ProductConfiguratorPopup.prototype, {
    setup() {
        super.setup(...arguments);
        this.availability = this.props.availability || {};
    },

    get validAttributeLineIds() {
        const lines = super.validAttributeLineIds;
        if (!this.availability) {
            return lines;
        }

        const hiddenLineIds = new Set((this.availability.hide_line_ids || []).map((id) => Number(id)));
        const allowedValueIdsByLine = this.availability.allowed_value_ids_by_line || {};

        return lines
            .filter((line) => !hiddenLineIds.has(line.id))
            .map((line) => {
                const allowedValueIds = getMappedValue(allowedValueIdsByLine, line.id);
                if (!Array.isArray(allowedValueIds)) {
                    return line;
                }

                const allowedSet = new Set(allowedValueIds.map((id) => Number(id)));
                const values = line.product_template_value_ids.filter((value) => allowedSet.has(value.id));
                return {
                    ...line,
                    product_template_value_ids: values,
                };
            })
            .filter((line) => line.product_template_value_ids.length > 0);
    },

    get isStockBlocked() {
        if (!this.availability) {
            return false;
        }

        if (this.availability.is_blocked) {
            return true;
        }

        const hasFilteredLines =
            Object.keys(this.availability.allowed_value_ids_by_line || {}).length > 0;

        return hasFilteredLines && this.validAttributeLineIds.length === 0;
    },

    get stockBlockedMessage() {
        return (
            this.availability?.message || _t("This product is out of stock in this POS location.")
        );
    },

    computePayload() {
        const payload = super.computePayload(...arguments);
        return applyAutoVendorSelection(payload, this.availability);
    },

    confirm() {
        if (this.isStockBlocked) {
            return;
        }
        return super.confirm(...arguments);
    },
});

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

function buildDefaultValuesFromAvailability(pos, availability) {
    if (!availability || !Array.isArray(availability.default_attribute_value_ids)) {
        return {};
    }
    const defaults = {};
    for (const valueIdRaw of availability.default_attribute_value_ids) {
        const valueId = normalizeId(valueIdRaw);
        if (!valueId) {
            continue;
        }
        const ptav = pos.data.models["product.template.attribute.value"].get(valueId);
        const lineId = ptav?.attribute_line_id?.id;
        if (lineId) {
            defaults[lineId] = valueId.toString();
        }
    }
    return defaults;
}

function applyImplicitSingleValueSelections(payload, availability) {
    if (!payload || !availability) {
        return payload;
    }

    const attributeValueIds = Array.isArray(payload.attribute_value_ids)
        ? payload.attribute_value_ids.map((value) => normalizeId(value)).filter(Boolean)
        : [];
    const allowedByLine = availability.allowed_value_ids_by_line || {};

    for (const lineId of Object.keys(allowedByLine)) {
        const allowedValueIds = getMappedValue(allowedByLine, lineId);
        if (!Array.isArray(allowedValueIds)) {
            continue;
        }
        const normalizedIds = allowedValueIds.map((value) => normalizeId(value)).filter(Boolean);
        if (normalizedIds.length === 1 && !attributeValueIds.includes(normalizedIds[0])) {
            attributeValueIds.push(normalizedIds[0]);
        }
    }

    payload.attribute_value_ids = attributeValueIds;
    return payload;
}

function applyAutoVendorSelection(payload, availability, enabled = true) {
    if (!payload || !availability) {
        return payload;
    }

    applyImplicitSingleValueSelections(payload, availability);

    if (!enabled) {
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

function mapToVariantValueIds(valueIds, availability) {
    if (!Array.isArray(valueIds) || !availability) {
        return [];
    }

    const mapped = [];
    const aliasMap = availability.variant_value_by_value_id || {};
    for (const valueIdRaw of valueIds) {
        const valueId = normalizeId(valueIdRaw);
        if (!valueId) {
            continue;
        }
        const mappedId = normalizeId(getMappedValue(aliasMap, valueId)) || valueId;
        if (!mapped.includes(mappedId)) {
            mapped.push(mappedId);
        }
    }
    return mapped;
}

ProductConfiguratorPopup.props = {
    ...ProductConfiguratorPopup.props,
    availability: { type: Object, optional: true },
    disableAutoVendor: { type: Boolean, optional: true },
};

patch(PosStore.prototype, {
    async openConfigurator(product, opts = {}) {
        let availability = {};
        const shouldAutoPickVendor = !opts.code;

        if (product?.raw?.product_tmpl_id && this.config?.id) {
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
            let defaultValues = buildDefaultValuesFromAvailability(this, availability);
            if (!Object.keys(defaultValues).length) {
                const match = product.barcode && product.barcode.includes(this.searchProductWord);
                if (this.searchProductWord && match) {
                    defaultValues = Object.fromEntries(
                        product.product_template_variant_value_ids.map((value) => [
                            value.attribute_line_id.id,
                            value.id.toString(),
                        ])
                    );
                }
            }

            const payload = await makeAwaitable(this.dialog, ProductConfiguratorPopup, {
                product: product,
                hideAlwaysVariants: opts.hideAlwaysVariants,
                defaultValues: defaultValues,
                availability: availability,
                disableAutoVendor: !shouldAutoPickVendor,
            });
            if (!payload) {
                return payload;
            }

            const preparedPayload = applyAutoVendorSelection(payload, availability, shouldAutoPickVendor);
            preparedPayload.attribute_value_ids = mapToVariantValueIds(
                preparedPayload.attribute_value_ids,
                availability
            );
            return preparedPayload;
        }

        const payload = {
            attribute_value_ids: attributeLinesValues.map((values) => values[0].id),
            attribute_custom_values: [],
            price_extra: attributeLinesValues
                .filter((attr) => attr[0].attribute_id.create_variant === "no_variant")
                .reduce((acc, values) => acc + values[0].price_extra, 0),
            quantity: 1,
        };

        const preparedPayload = applyAutoVendorSelection(payload, availability, shouldAutoPickVendor);
        preparedPayload.attribute_value_ids = mapToVariantValueIds(
            preparedPayload.attribute_value_ids,
            availability
        );
        return preparedPayload;
    },
});

patch(BaseProductAttribute.prototype, {
    setup() {
        super.setup(...arguments);
        if (!this.values?.length || this.attributeLine.attribute_id.display_type === "multi") {
            return;
        }

        const selectedValueId = parseInt(this.state.attribute_value_ids, 10);
        const firstSellableValue = this.values.find((value) => !value.excluded);
        const fallbackValue = firstSellableValue || this.values[0];
        const selectedValue = this.values.find((value) => value.id === selectedValueId);
        const shouldReplaceSelected = !selectedValue || selectedValue.excluded;
        if (shouldReplaceSelected && fallbackValue) {
            this.state.attribute_value_ids = fallbackValue.id.toString();
        }
    },
});

patch(ProductConfiguratorPopup.prototype, {
    setup() {
        super.setup(...arguments);
        this.availability = this.props.availability || {};
        this.disableAutoVendor = Boolean(this.props.disableAutoVendor);
        this.isTrackedProduct = Boolean(this.availability.is_tracked_product);
        this.variantLineIds = new Set(
            (this.availability.variant_line_ids || []).map((lineId) => Number(lineId))
        );
        this.variantAttributeIds = new Set(
            (this.availability.variant_attribute_ids || []).map((attributeId) => Number(attributeId))
        );
    },

    get validAttributeLineIds() {
        const lines = super.validAttributeLineIds;
        if (!this.availability) {
            return lines;
        }

        const hiddenLineIds = new Set((this.availability.hide_line_ids || []).map((id) => Number(id)));
        const allowedValueIdsByLine = this.availability.allowed_value_ids_by_line || {};

        const processedLines = lines
            .filter((line) => !hiddenLineIds.has(line.id))
            .map((line) => {
                const allowedValueIds = getMappedValue(allowedValueIdsByLine, line.id);
                if (!Array.isArray(allowedValueIds)) {
                    return line;
                }

                const allowedSet = new Set(allowedValueIds.map((id) => Number(id)));
                const values = this.isTrackedProduct
                    ? line.product_template_value_ids.map((value) => ({
                          ...value,
                          excluded: value.excluded || !allowedSet.has(value.id),
                      }))
                    : line.product_template_value_ids.filter((value) => allowedSet.has(value.id));
                return {
                    ...line,
                    product_template_value_ids: values,
                };
            });

        return this.isTrackedProduct
            ? processedLines
            : processedLines.filter((line) => line.product_template_value_ids.length > 0);
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

        if (hasFilteredLines && this.validAttributeLineIds.length === 0) {
            return true;
        }

        const selectedValueIds = this.getVariantAttributeValueIds();
        for (const valueId of selectedValueIds) {
            const ptav = this.pos.data.models["product.template.attribute.value"].get(valueId);
            const lineId = ptav?.attribute_line_id?.id;
            if (!lineId) {
                continue;
            }
            const allowedValueIds = getMappedValue(this.availability.allowed_value_ids_by_line, lineId);
            if (!Array.isArray(allowedValueIds) || !allowedValueIds.length) {
                continue;
            }
            const allowedSet = new Set(allowedValueIds.map((id) => Number(id)));
            if (!allowedSet.has(Number(valueId))) {
                return true;
            }
        }

        return false;
    },

    get stockBlockedMessage() {
        return (
            this.availability?.message || _t("This product is out of stock in this POS location.")
        );
    },

    computePayload() {
        const payload = super.computePayload(...arguments);
        const preparedPayload = applyAutoVendorSelection(
            payload,
            this.availability,
            !this.disableAutoVendor
        );
        preparedPayload.attribute_value_ids = mapToVariantValueIds(
            preparedPayload.attribute_value_ids,
            this.availability
        );
        return preparedPayload;
    },

    getVariantAttributeValueIds() {
        const mappedValueIds = mapToVariantValueIds(
            super.getVariantAttributeValueIds(...arguments),
            this.availability
        );
        return mappedValueIds.filter((valueId) => {
            const ptav = this.pos.data.models["product.template.attribute.value"].get(valueId);
            if (!ptav) {
                return true;
            }

            const attributeId = ptav.attribute_id?.id || ptav.attribute_line_id?.attribute_id?.id;
            if (this.variantAttributeIds.size && attributeId) {
                return this.variantAttributeIds.has(attributeId);
            }

            const lineId = ptav.attribute_line_id?.id;
            if (this.variantLineIds.size && lineId) {
                return this.variantLineIds.has(lineId);
            }

            return true;
        });
    },

    confirm() {
        if (this.isStockBlocked) {
            return;
        }
        return super.confirm(...arguments);
    },
});

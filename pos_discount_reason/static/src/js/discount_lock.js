/** @odoo-module */

import { PosOrder } from "@point_of_sale/app/models/pos_order";
import { PosOrderline } from "@point_of_sale/app/models/pos_order_line";
import { patch } from "@web/core/utils/patch";
import { rpc } from "@web/core/network/rpc";

function asId(value) {
    if (Array.isArray(value)) {
        return value[0] || false;
    }
    if (value && typeof value === "object") {
        return value.id || false;
    }
    return value || false;
}

function buildCategoryParentMap(pos) {
    const parentMap = new Map();
    const categoryModel = pos?.models?.["product.category"];
    const categories = categoryModel ? categoryModel.getAll() : [];

    categories.forEach((category) => {
        parentMap.set(category.id, asId(category.parent_id));
    });

    return parentMap;
}

function getProductFamilyId(product, order = null) {
    if (!product) {
        return false;
    }
    const rawFamilyId = asId(product.family_id);
    if (rawFamilyId) {
        return rawFamilyId;
    }
    const productId = asId(product.id);
    if (!order || !productId) {
        return false;
    }
    const cache = order._discount_reason_product_family_by_product_id || {};
    return asId(cache[productId]);
}

function getCategoryChain(categoryId, parentMap) {
    const chain = [];
    const visited = new Set();
    let currentId = categoryId;

    while (currentId && !visited.has(currentId)) {
        chain.push(currentId);
        visited.add(currentId);
        currentId = parentMap.get(currentId);
    }

    return chain;
}

function getReasonById(pos, reasonId) {
    const reasonModel = pos?.models?.["discount.reason"];
    if (!reasonModel || !reasonId) {
        return null;
    }
    return reasonModel.getBy("id", reasonId) || null;
}

function getReasonCategoryRules(pos, reasonId) {
    const order = pos?.get_order ? pos.get_order() : null;
    const cachedRules = order?._discount_reason_rule_lines_by_reason?.[reasonId];
    if (cachedRules && cachedRules.length) {
        return cachedRules;
    }

    const linesModel = pos?.models?.["discount.reason.category.line"];
    if (!linesModel || !reasonId) {
        return [];
    }

    return linesModel
        .getAll()
        .filter((line) => asId(line.discount_reason_id) === reasonId)
        .map((line) => ({
            sequence: Number(line.sequence || 10),
            discount_percentage: Number(line.discount_percentage || 0),
            category_ids: (line.category_ids || []).map((categoryId) => asId(categoryId)).filter(Boolean),
            family_ids: (line.family_ids || []).map((familyId) => asId(familyId)).filter(Boolean),
        }))
        .filter((line) => line.category_ids.length > 0)
        .sort((a, b) => a.sequence - b.sequence);
}

function getMatchedCategoryDiscount(product, rules, parentMap, order = null) {
    if (!product || !rules.length) {
        return null;
    }

    const categoryId = asId(product.categ_id);
    if (!categoryId) {
        return null;
    }

    const categoryChain = getCategoryChain(categoryId, parentMap);
    const productFamilyId = getProductFamilyId(product, order);
    let bestMatch = null;

    rules.forEach((rule) => {
        rule.category_ids.forEach((ruleCategoryId) => {
            const depth = categoryChain.indexOf(ruleCategoryId);
            if (depth === -1) {
                return;
            }

            const hasFamilyScope = Boolean(rule.family_ids.length);
            if (hasFamilyScope && (!productFamilyId || !rule.family_ids.includes(productFamilyId))) {
                return;
            }

            const specificity = hasFamilyScope ? 0 : 1;
            if (
                !bestMatch ||
                specificity < bestMatch.specificity ||
                (specificity === bestMatch.specificity && depth < bestMatch.depth) ||
                (
                    specificity === bestMatch.specificity &&
                    depth === bestMatch.depth &&
                    rule.sequence < bestMatch.sequence
                )
            ) {
                bestMatch = {
                    specificity,
                    depth,
                    sequence: rule.sequence,
                    discount: rule.discount_percentage,
                };
            }
        });
    });

    return bestMatch ? bestMatch.discount : null;
}

async function hydrateProductFamiliesForOrder(order, productIds) {
    if (!order || !productIds?.length) {
        return;
    }
    const response = await rpc(
        "/web/dataset/call_kw/pos.order/get_products_family_map_pos",
        {
            model: "pos.order",
            method: "get_products_family_map_pos",
            args: [productIds],
            kwargs: {},
        }
    );
    if (!order._discount_reason_product_family_by_product_id) {
        order._discount_reason_product_family_by_product_id = {};
    }
    Object.entries(response || {}).forEach(([productId, familyId]) => {
        const pid = Number(productId);
        if (Number.isFinite(pid)) {
            order._discount_reason_product_family_by_product_id[pid] = asId(familyId);
        }
    });
}

async function hydrateLineFamilyAndRefreshDiscount(order, line) {
    const productId = asId(line?.product_id?.id || line?.product_id);
    if (!order || !line || !productId) {
        return;
    }
    await hydrateProductFamiliesForOrder(order, [productId]);
    const cap = line._getDiscountCap ? line._getDiscountCap() : null;
    if (cap !== null) {
        line.set_discount(cap);
    }
}

patch(PosOrder.prototype, {
    add_product(product, options) {
        const existingLines = new Set(this.get_orderlines());
        let result;
        if (typeof super.add_product === "function") {
            result = super.add_product(product, options);
        } else if (typeof super.addProduct === "function") {
            result = super.addProduct(product, options);
        } else if (typeof this.addProduct === "function") {
            result = this.addProduct(product, options);
        } else {
            return result;
        }
        const selectedLine = this.get_selected_orderline();
        const reasonId = asId(this.discount_reason_id);

        if (!selectedLine || !reasonId) {
            return result;
        }

        // Preserve operator-edited discounts when quantity merges into an existing line.
        if (existingLines.has(selectedLine)) {
            return result;
        }

        const cap = selectedLine._getDiscountCap ? selectedLine._getDiscountCap() : null;
        if (cap === null) {
            return result;
        }

        const reasonValue = this.discount_reason_id;
        const pos = this.pos || this.env?.services?.pos;
        const reason = (reasonValue && typeof reasonValue === "object")
            ? reasonValue
            : getReasonById(pos, reasonId);
        if (reason?.discount_type === "fixed_amount") {
            return result;
        }

        selectedLine.set_discount(cap);

        const rules = this._discount_reason_rule_lines_by_reason?.[reasonId] || [];
        const hasFamilyScopedRules = rules.some((rule) => (rule.family_ids || []).length > 0);
        if (reason?.use_category_discount && hasFamilyScopedRules && !getProductFamilyId(selectedLine.product_id, this)) {
            hydrateLineFamilyAndRefreshDiscount(this, selectedLine).catch((error) => {
                console.warn("Failed to hydrate product family for POS discount matching.", error);
            });
        }

        return result;
    },
});

patch(PosOrderline.prototype, {
    _getDiscountCap() {
        const order = this.order_id;
        if (!order) {
            return null;
        }

        const reasonValue = order.discount_reason_id;
        const reasonId = asId(reasonValue);
        if (!reasonId) {
            return null;
        }

        const pos = this.env?.services?.pos || order.pos;
        const reason = (reasonValue && typeof reasonValue === "object")
            ? reasonValue
            : getReasonById(pos, reasonId);
        if (!reason) {
            return null;
        }

        if (reason.discount_type === "fixed_amount") {
            return 100;
        }

        const reasonCap = Number(reason.discount_percentage || 0);
        if (!reason.use_category_discount) {
            return reasonCap;
        }

        const cachedRules = order?._discount_reason_rule_lines_by_reason?.[reasonId];
        if (!cachedRules || !cachedRules.length) {
            // Category mode requires fresh rules loaded when reason is selected.
            // Avoid falling back to potentially stale POS boot cache.
            return 0;
        }

        const rules = cachedRules;
        if (!rules.length) {
            return 0;
        }

        const product = this.product_id;
        const parentMap = buildCategoryParentMap(pos);
        const matchedDiscount = getMatchedCategoryDiscount(product, rules, parentMap, order);
        return matchedDiscount !== null ? matchedDiscount : 0;
    },

    set_discount(discount) {
        const cap = this._getDiscountCap();
        if (cap === null) {
            return super.set_discount(discount);
        }
        const requested = Number(discount || 0);
        const normalized = Number.isFinite(requested) ? requested : 0;
        const clamped = Math.max(0, Math.min(normalized, cap));
        return super.set_discount(clamped);
    },
});

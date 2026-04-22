/** @odoo-module */

import { PosOrderline } from "@point_of_sale/app/models/pos_order_line";
import { patch } from "@web/core/utils/patch";

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
        }))
        .filter((line) => line.category_ids.length > 0)
        .sort((a, b) => a.sequence - b.sequence);
}

function getMatchedCategoryDiscount(product, rules, parentMap) {
    if (!product || !rules.length) {
        return null;
    }

    const categoryId = asId(product.categ_id);
    if (!categoryId) {
        return null;
    }

    const categoryChain = getCategoryChain(categoryId, parentMap);
    let bestMatch = null;

    rules.forEach((rule) => {
        rule.category_ids.forEach((ruleCategoryId) => {
            const depth = categoryChain.indexOf(ruleCategoryId);
            if (depth === -1) {
                return;
            }

            if (
                !bestMatch ||
                depth < bestMatch.depth ||
                (depth === bestMatch.depth && rule.sequence < bestMatch.sequence)
            ) {
                bestMatch = {
                    depth,
                    sequence: rule.sequence,
                    discount: rule.discount_percentage,
                };
            }
        });
    });

    return bestMatch ? bestMatch.discount : null;
}

patch(PosOrderline.prototype, {
    _getLockedCategoryDiscount() {
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

        const rules = getReasonCategoryRules(pos, reasonId);
        if (!reason.use_category_discount || !rules.length) {
            return null;
        }

        const product = this.product_id;
        const parentMap = buildCategoryParentMap(pos);
        const matchedDiscount = getMatchedCategoryDiscount(product, rules, parentMap);
        const defaultDiscount = Number(reason.discount_percentage || 0);
        return matchedDiscount !== null ? matchedDiscount : defaultDiscount;
    },

    set_discount(discount) {
        const lockedDiscount = this._getLockedCategoryDiscount();
        if (lockedDiscount === null) {
            return super.set_discount(discount);
        }
        return super.set_discount(lockedDiscount);
    },
});

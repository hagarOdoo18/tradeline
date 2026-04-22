/** @odoo-module */

import { ControlButtons } from "@point_of_sale/app/screens/product_screen/control_buttons/control_buttons";
import { patch } from "@web/core/utils/patch";
import { _t } from "@web/core/l10n/translation";
import { SelectionPopup } from "@point_of_sale/app/utils/input_popups/selection_popup";
import { TextInputPopup } from "@point_of_sale/app/utils/input_popups/text_input_popup";
import { makeAwaitable } from "@point_of_sale/app/store/make_awaitable_dialog";
import { AlertDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { rpc } from "@web/core/network/rpc";

patch(ControlButtons.prototype, {
    setup() {
        super.setup();
        console.log("ControlButtons patched successfully!");
    },
    _asId(value) {
        if (Array.isArray(value)) {
            return value[0] || false;
        }
        if (value && typeof value === "object") {
            return value.id || false;
        }
        return value || false;
    },

    _buildCategoryParentMap() {
        const parentMap = new Map();
        const categoryModel = this.pos.models && this.pos.models["product.category"];
        const categories = categoryModel ? categoryModel.getAll() : [];

        categories.forEach((category) => {
            parentMap.set(category.id, this._asId(category.parent_id));
        });

        return parentMap;
    },

    _getProductFamilyId(product, order = null) {
        if (!product) {
            return false;
        }
        const rawFamilyId = this._asId(product.family_id);
        if (rawFamilyId) {
            return rawFamilyId;
        }

        const productId = this._asId(product.id);
        if (!order || !productId) {
            return false;
        }

        const cache = order._discount_reason_product_family_by_product_id || {};
        return this._asId(cache[productId]);
    },

    _getCategoryChain(categoryId, parentMap) {
        const chain = [];
        const visited = new Set();
        let currentId = categoryId;

        while (currentId && !visited.has(currentId)) {
            chain.push(currentId);
            visited.add(currentId);
            currentId = parentMap.get(currentId);
        }

        return chain;
    },

    _getReasonCategoryRules(reasonId) {
        const order = this.pos.get_order();
        const cachedRules = order?._discount_reason_rule_lines_by_reason?.[reasonId];
        if (cachedRules && cachedRules.length) {
            return cachedRules;
        }

        const linesModel = this.pos.models && this.pos.models["discount.reason.category.line"];
        if (!linesModel || !reasonId) {
            return [];
        }

        return linesModel
            .getAll()
            .filter((line) => this._asId(line.discount_reason_id) === reasonId)
            .map((line) => ({
                id: this._asId(line.id),
                sequence: Number(line.sequence || 10),
                discount_percentage: Number(line.discount_percentage || 0),
                category_ids: (line.category_ids || []).map((categoryId) => this._asId(categoryId)).filter(Boolean),
                family_ids: (line.family_ids || []).map((familyId) => this._asId(familyId)).filter(Boolean),
            }))
            .filter((line) => line.category_ids.length)
            .sort((a, b) => a.sequence - b.sequence);
    },

    async _fetchReasonCategoryRulesFromServer(reasonId) {
        if (!reasonId) {
            return [];
        }

        const rows = await rpc(
            "/web/dataset/call_kw/pos.order/get_discount_reason_rules_pos",
            {
                model: "pos.order",
                method: "get_discount_reason_rules_pos",
                args: [reasonId],
                kwargs: {},
            }
        );

        return rows
            .map((line) => ({
                id: this._asId(line.id),
                sequence: Number(line.sequence || 10),
                discount_percentage: Number(line.discount_percentage || 0),
                category_ids: (line.category_ids || []).map((categoryId) => this._asId(categoryId)).filter(Boolean),
                family_ids: (line.family_ids || []).map((familyId) => this._asId(familyId)).filter(Boolean),
            }))
            .filter((line) => line.category_ids.length)
            .sort((a, b) => a.sequence - b.sequence);
    },

    _setOrderReasonRules(order, reasonId, rules) {
        if (!order || !reasonId || !Array.isArray(rules)) {
            return;
        }
        if (!order._discount_reason_rule_lines_by_reason) {
            order._discount_reason_rule_lines_by_reason = {};
        }
        order._discount_reason_rule_lines_by_reason[reasonId] = rules;
    },

    _setOrderProductFamilyCache(order, mapping) {
        if (!order || !mapping || typeof mapping !== "object") {
            return;
        }
        if (!order._discount_reason_product_family_by_product_id) {
            order._discount_reason_product_family_by_product_id = {};
        }

        Object.entries(mapping).forEach(([productId, familyId]) => {
            const pid = Number(productId);
            if (Number.isFinite(pid)) {
                order._discount_reason_product_family_by_product_id[pid] = this._asId(familyId);
            }
        });
    },

    _collectMissingFamilyProductIds(order, orderlines) {
        if (!order || !orderlines?.length) {
            return [];
        }

        const missingIds = [];
        orderlines.forEach((line) => {
            const product = line.get_product();
            const productId = this._asId(product?.id);
            if (!productId) {
                return;
            }
            const familyId = this._getProductFamilyId(product, order);
            if (!familyId) {
                missingIds.push(productId);
            }
        });

        return [...new Set(missingIds)];
    },

    async _fetchProductFamiliesFromServer(productIds) {
        if (!productIds?.length) {
            return {};
        }

        const mapping = await rpc(
            "/web/dataset/call_kw/pos.order/get_products_family_map_pos",
            {
                model: "pos.order",
                method: "get_products_family_map_pos",
                args: [productIds],
                kwargs: {},
            }
        );
        return mapping || {};
    },

    _getReasonScopeLabels(rules) {
        const categoryModel = this.pos.models && this.pos.models["product.category"];
        const familyModel = this.pos.models && this.pos.models["product.family"];
        if (!categoryModel || !rules.length) {
            return [];
        }

        return rules.map((rule) => {
            const categories = rule.category_ids
                .map((id) => categoryModel.getBy("id", id))
                .filter(Boolean)
                .map((category) => category.display_name || category.name)
                .join(", ");

            if (!rule.family_ids.length) {
                return categories;
            }

            const families = rule.family_ids
                .map((id) => {
                    const family = familyModel?.getBy("id", id);
                    if (family) {
                        return family.display_name || family.name;
                    }
                    return `#${id}`;
                })
                .join(", ");

            return `${categories} [${_t("Families")}: ${families}]`;
        });
    },

    _isDiscountDebugEnabled() {
        try {
            return window.location.search.includes("debug=1");
        } catch {
            return false;
        }
    },

    _errorToText(error) {
        if (!error) {
            return "unknown error";
        }
        const message = error.message || error.toString();
        const serverMessage = error?.data?.message || error?.data?.debug || "";
        if (serverMessage) {
            return `${message} | ${serverMessage}`;
        }
        return message;
    },

    _getCategoryNameById(categoryId) {
        const categoryModel = this.pos.models && this.pos.models["product.category"];
        if (!categoryModel || !categoryId) {
            return String(categoryId || "");
        }
        const category = categoryModel.getBy("id", categoryId);
        return (category && (category.display_name || category.name)) || String(categoryId);
    },

    _getFamilyNameById(familyId) {
        const familyModel = this.pos.models && this.pos.models["product.family"];
        if (!familyModel || !familyId) {
            return String(familyId || "");
        }
        const family = familyModel.getBy("id", familyId);
        return (family && (family.display_name || family.name)) || String(familyId);
    },

    _buildDiscountDebugReport(reason, lineResults, usedFallback, fallbackErrorText) {
        const header = [
            `${_t("Reason")}: ${reason.name}`,
            `${_t("Fallback Used")}: ${usedFallback ? _t("Yes") : _t("No")}`,
            fallbackErrorText ? `${_t("Refresh Error")}: ${fallbackErrorText}` : "",
            "",
        ].filter(Boolean);

        const body = lineResults.map((item, idx) => {
            const familyName = item.productFamilyId ? this._getFamilyNameById(item.productFamilyId) : _t("None");
            if (!item.match) {
                return `${idx + 1}. ${item.productName}: ${_t("NO MATCH")} (cat=${item.productCategoryName}, family=${familyName})`;
            }
            const ruleFamilies = item.match.family_ids.length
                ? item.match.family_ids.map((id) => this._getFamilyNameById(id)).join(", ")
                : _t("None");
            return `${idx + 1}. ${item.productName}: ${item.match.discount}% (rule#${item.match.id || "n/a"}, cat=${item.productCategoryName}, family=${familyName}, ruleFamilies=${ruleFamilies})`;
        });

        return header.concat(body).join("\n");
    },

    _getMatchedCategoryRule(product, rules, parentMap, order = null) {
        if (!product || !rules.length) {
            return null;
        }

        const categoryId = this._asId(product.categ_id);
        if (!categoryId) {
            return null;
        }

        const categoryChain = this._getCategoryChain(categoryId, parentMap);
        const productFamilyId = this._getProductFamilyId(product, order);
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
                        id: rule.id || null,
                        specificity,
                        depth,
                        sequence: rule.sequence,
                        discount: rule.discount_percentage,
                        family_ids: rule.family_ids || [],
                    };
                }
            });
        });

        return bestMatch;
    },

    _getMatchedCategoryDiscount(product, rules, parentMap, order = null) {
        const match = this._getMatchedCategoryRule(product, rules, parentMap, order);
        return match ? match.discount : null;
    },

    _applyDiscountByReason(order, reason, forcedRules = null, options = {}) {
        const orderlines = order.get_orderlines();
        const defaultDiscount = Number(reason.discount_percentage || 0);
        const useCategoryDiscount = Boolean(reason.use_category_discount);
        const usedFallback = Boolean(options.usedFallback);
        const fallbackErrorText = options.fallbackErrorText || "";

        if (!orderlines.length) {
            return { ok: true };
        }

        if (!useCategoryDiscount) {
            orderlines.forEach((line) => line.set_discount(defaultDiscount));
            return { ok: true };
        }

        const reasonId = this._asId(reason.id);
        const rules = forcedRules && forcedRules.length
            ? forcedRules
            : this._getReasonCategoryRules(reasonId);
        if (!rules.length) {
            return {
                ok: false,
                message: _t("This discount reason requires category rules but none are configured."),
            };
        }

        const parentMap = this._buildCategoryParentMap();
        const matchedLines = [];
        const unmatchedLines = [];
        const lineResults = [];
        orderlines.forEach((line) => {
            const product = line.get_product();
            const match = this._getMatchedCategoryRule(product, rules, parentMap, order);
            const productCategoryId = this._asId(product?.categ_id);
            const productFamilyId = this._getProductFamilyId(product, order);
            lineResults.push({
                productName: product?.display_name || product?.name || _t("Unknown Product"),
                productCategoryName: this._getCategoryNameById(productCategoryId),
                productFamilyId,
                match,
            });

            if (match === null) {
                unmatchedLines.push(line);
                return;
            }
            matchedLines.push({ line, discount: match.discount });
        });

        if (!matchedLines.length) {
            const allowedScope = this._getReasonScopeLabels(rules);
            return {
                ok: false,
                message: _t("No order lines are eligible for this discount reason. Allowed scope: ") +
                    allowedScope.join("; ") +
                    ". " +
                    _t("Remove discount reason or add eligible products."),
            };
        }

        // Auto-fill to max configured for each matched category, cashier can reduce later.
        matchedLines.forEach(({ line, discount }) => line.set_discount(discount));
        // Ignore non-eligible lines for this reason.
        unmatchedLines.forEach((line) => line.set_discount(0));

        const result = { ok: true };
        if (this._isDiscountDebugEnabled()) {
            result.debugReport = this._buildDiscountDebugReport(
                reason,
                lineResults,
                usedFallback,
                fallbackErrorText
            );
        }
        return result;
    },

    // Add Discount Reason Button Function
    async addDiscountReason() {
        console.log("addDiscountReason clicked!");

        const order = this.pos.get_order();
        console.log("Current order:", order);

        if (!order) {
            console.log("No order found, showing alert");
            this.dialog.add(AlertDialog, {
                title: _t("No Order"),
                body: _t("No active order found."),
            });
            return;
        }

        try {
            console.log("Getting discount reasons from loaded data...");
            console.log("Available models:", this.pos.models);

            // Get discount reasons from loaded POS data
            let discountReasons = [];

            if (this.pos.models && this.pos.models['discount.reason']) {
                console.log("Using discount.reason from POS models");
                discountReasons = this.pos.models['discount.reason'].getAll();
                console.log("Discount reasons from POS data:", discountReasons);
            } else {
                console.log("No discount.reason data found in POS");
            }

            if (!discountReasons.length) {
                console.log("No predefined reasons, showing text input");

                // If no predefined reasons, allow free text
                const confirmed = await makeAwaitable(this.dialog, TextInputPopup, {
                    title: _t("Discount Reason"),
                    startingValue: order.discount_reason || "",
                    placeholder: _t("Enter discount reason..."),
                });

                console.log("Text input result:", confirmed);

                if (confirmed) {
                    order.discount_reason = confirmed;
                    // Use the correct way to update order in Odoo 18
                    this.pos.selectedOrder = order;
                    console.log("Discount reason set to:", confirmed);
                }
                return;
            }

            // Show selection of predefined reasons
            const reasonList = discountReasons.map((reason) => ({
                id: reason.id,
                item: reason,
                label: reason.use_category_discount
                    ? `${reason.name} (${_t("By Category Caps")})`
                    : `${reason.name} (${reason.discount_percentage}%)`,
                isSelected: false,
            }));

//            // Add custom option
//            reasonList.push({
//                id: 'custom',
//                item: { name: 'Custom', discount_percentage: 0 },
//                label: _t("Custom Reason"),
//                isSelected: false,
//            });

            console.log("Showing selection popup with options:", reasonList);

            const confirmed = await makeAwaitable(this.dialog, SelectionPopup, {
                title: _t("Select Discount Reason"),
                list: reasonList,
            });

            console.log("Selection result:", confirmed);

            if (confirmed) {
                if (confirmed.id === 'custom') {
                    const customReason = await makeAwaitable(this.dialog, TextInputPopup, {
                        title: _t("Custom Discount Reason"),
                        placeholder: _t("Enter custom reason..."),
                    });
                    if (customReason) {
                        order.discount_reason = customReason;
//                        this.pos.selectedOrder = order;
                        console.log("Custom discount reason set to:", customReason);
                    }
                } else {
                    const reasonId = this._asId(confirmed.id);
                    let liveRules = [];
                    let usedFallback = false;
                    let fallbackErrorText = "";
                    if (confirmed.use_category_discount && reasonId) {
                        try {
                            liveRules = await this._fetchReasonCategoryRulesFromServer(reasonId);
                        } catch (error) {
                            fallbackErrorText = this._errorToText(error);
                            console.warn("Failed to fetch latest discount rules from server.", error);
                            liveRules = this._getReasonCategoryRules(reasonId);
                            usedFallback = true;
                        }

                        if (!liveRules.length) {
                            this.dialog.add(AlertDialog, {
                                title: _t("Invalid Discount Reason"),
                                body: _t("This discount reason requires category rules but none are configured.") +
                                    (fallbackErrorText ? `\n\n${_t("Refresh Error")}: ${fallbackErrorText}` : ""),
                            });
                            return;
                        }

                        this._setOrderReasonRules(order, reasonId, liveRules);

                        const missingFamilyProductIds = this._collectMissingFamilyProductIds(
                            order,
                            order.get_orderlines()
                        );
                        if (missingFamilyProductIds.length) {
                            try {
                                const familyMap = await this._fetchProductFamiliesFromServer(
                                    missingFamilyProductIds
                                );
                                this._setOrderProductFamilyCache(order, familyMap);
                            } catch (error) {
                                console.warn(
                                    "Failed to hydrate missing product families for discount matching.",
                                    error
                                );
                            }
                        }
                    }

                    const applyResult = this._applyDiscountByReason(order, confirmed, liveRules, {
                        usedFallback,
                        fallbackErrorText,
                    });
                    if (!applyResult.ok) {
                        this.dialog.add(AlertDialog, {
                            title: _t("Invalid Discount Reason"),
                            body: applyResult.message,
                        });
                        return;
                    }

                    if (applyResult.debugReport) {
                        this.dialog.add(AlertDialog, {
                            title: _t("Discount Debug"),
                            body: applyResult.debugReport,
                        });
                    }

                    order.discount_reason_id = confirmed;
                    console.log("Discount reason set to:", confirmed.name);
                }
            }
        } catch (error) {
            console.error("Error in addDiscountReason:", error);
            this.dialog.add(AlertDialog, {
                title: _t("Error"),
                body: _t("Failed to add discount reason: ") + error.message,
            });
        }
    },

    // Toggle As Gift Button Function
    async toggleAsGift() {
        console.log("toggleAsGift clicked!");

        const order = this.pos.get_order();
        console.log("Current order:", order);

        if (!order) {
            console.log("No order found");
            this.dialog.add(AlertDialog, {
                title: _t("No Order"),
                body: _t("No active order found."),
            });
            return;
        }

        try {
            const currentStatus = order.as_gift ? _t("Gift Order") : _t("Normal Order");
            console.log("Current status:", currentStatus);

            // Use SelectionPopup instead of ConfirmPopup
            const options = [
                {
                    id: true,
                    item: true,
                    label: _t("Mark as Gift"),
                    isSelected: !order.as_gift,
                },
                {
                    id: false,
                    item: false,
                    label: _t("Normal Order"),
                    isSelected: order.as_gift,
                }
            ];

            console.log("Showing gift options:", options);

            const confirmed = await makeAwaitable(this.dialog, SelectionPopup, {
                title: _t("Order Type"),
//                body: _t(`Current: ${currentStatus}`),
                list: options,
            });

            console.log("Gift selection result:", confirmed);

            if (confirmed !== undefined) {
                order.update({
                    as_gift: Boolean(confirmed),
                });
                console.log("As gift set to:", Boolean(confirmed));
            }
        } catch (error) {
            console.error("Error in toggleAsGift:", error);
        }
    },

    // Select Sales Rep Button Function
    async selectSalesRep() {
        console.log("selectSalesRep clicked!");

        const order = this.pos.get_order();
        console.log("Current order:", order);

        if (!order) {
            console.log("No order found");
            this.dialog.add(AlertDialog, {
                title: _t("No Order"),
                body: _t("No active order found."),
            });
            return;
        }

        try {
            console.log("Getting sales reps from loaded data...");
            console.log("Available models:", this.pos.models);

            // Get sales reps from loaded POS data
            let salesReps = [];

            // FIXED: كان في خطأ هنا - كنت بستخدم this.models بدلاً من this.pos.models
            if (this.pos.models && this.pos.models['sales.rep']) {
                console.log("Using sales.rep from POS models");
                salesReps = this.pos.models['sales.rep'].getAll();
                console.log("Sales reps from POS data:", salesReps);
            } else {
                console.log("No sales.rep model found, trying sample data...");

                // Sample data for testing
                salesReps = [
                    {
                        id: 1,
                        name: 'Sales Rep 1',
                        code: 'SR001',
                        type: 'sales',
                        mail: 'sales1@company.com'
                    },
                    {
                        id: 2,
                        name: 'Sales Rep 2',
                        code: 'SR002',
                        type: 'sales',
                        mail: 'sales2@company.com'
                    }
                ];
            }

            console.log("Final sales reps list:", salesReps);

            if (!salesReps.length) {
                this.dialog.add(AlertDialog, {
                    title: _t("No Sales Representatives"),
                    body: _t("No sales representatives found. Please add some in the Sales Rep menu first."),
                });
                return;
            }

            const repList = salesReps.map((rep) => {
                let label = rep.name;
                if (rep.code) {
                    label = `[${rep.code}] ${rep.name}`;
                }
                if (rep.type) {
                    label += ` (${rep.type})`;
                }

                return {
                    id: rep.id,
                    item: rep,
                    label: label,
                    isSelected: order.sales_rep_id && order.sales_rep_id.id === rep.id,
                };
            });

            console.log("Showing sales rep selection:", repList);

            const confirmed = await makeAwaitable(this.dialog, SelectionPopup, {
                title: _t("Select Sales Representative"),
                list: repList,
            });

            console.log("Sales rep selection result:", confirmed);

            if (confirmed) {
                order.sales_rep_id = confirmed;


//                this.pos.selectedOrder = order;
                console.log("Sales rep set to:",order.sales_rep_id);
            }
        } catch (error) {
            console.error("Error in selectSalesRep:", error);
            this.dialog.add(AlertDialog, {
                title: _t("Error"),
                body: _t("Failed to load sales representatives: ") + error.message,
            });
        }
    }
});

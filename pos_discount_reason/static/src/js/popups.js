/** @odoo-module */

import { ControlButtons } from "@point_of_sale/app/screens/product_screen/control_buttons/control_buttons";
import { patch } from "@web/core/utils/patch";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { SelectionPopup } from "@point_of_sale/app/utils/input_popups/selection_popup";
import { TextInputPopup } from "@point_of_sale/app/utils/input_popups/text_input_popup";
import { makeAwaitable } from "@point_of_sale/app/store/make_awaitable_dialog";
import { AlertDialog } from "@web/core/confirmation_dialog/confirmation_dialog";

patch(ControlButtons.prototype, {
    setup() {
        super.setup();
        this.orm = useService("orm");
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
        const linesModel = this.pos.models && this.pos.models["discount.reason.category.line"];
        if (!linesModel || !reasonId) {
            return [];
        }

        return linesModel
            .getAll()
            .filter((line) => this._asId(line.discount_reason_id) === reasonId)
            .map((line) => ({
                sequence: Number(line.sequence || 10),
                discount_percentage: Number(line.discount_percentage || 0),
                category_ids: (line.category_ids || []).map((categoryId) => this._asId(categoryId)).filter(Boolean),
            }))
            .sort((a, b) => a.sequence - b.sequence);
    },

    _getMatchedCategoryDiscount(product, rules, parentMap) {
        if (!product || !rules.length) {
            return null;
        }

        const categoryId = this._asId(product.categ_id);
        if (!categoryId) {
            return null;
        }

        const categoryChain = this._getCategoryChain(categoryId, parentMap);
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
    },

    _applyDiscountByReason(order, reason) {
        const orderlines = order.get_orderlines();
        const defaultDiscount = Number(reason.discount_percentage || 0);
        const useCategoryDiscount = Boolean(reason.use_category_discount);

        if (!orderlines.length) {
            return;
        }

        if (!useCategoryDiscount) {
            orderlines.forEach((line) => line.set_discount(defaultDiscount));
            return;
        }

        const reasonId = this._asId(reason.id);
        const rules = this._getReasonCategoryRules(reasonId);
        if (!rules.length) {
            orderlines.forEach((line) => line.set_discount(defaultDiscount));
            return;
        }

        const parentMap = this._buildCategoryParentMap();
        orderlines.forEach((line) => {
            const product = line.get_product();
            const matchedDiscount = this._getMatchedCategoryDiscount(product, rules, parentMap);
            line.set_discount(matchedDiscount !== null ? matchedDiscount : defaultDiscount);
        });
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
                    ? `${reason.name} (${reason.discount_percentage}% max)`
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
                    order.discount_reason_id = confirmed;
//                    this.pos.selectedOrder = order;
                    console.log("Discount reason set to:", confirmed.name);
                    console.log("Discount reason set to: ezzat", order.discount_reason_id);

                    this._applyDiscountByReason(order, confirmed);
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
                order.as_gift = confirmed;
//                this.pos.selectedOrder = order;
                console.log("As gift set to:", confirmed);
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

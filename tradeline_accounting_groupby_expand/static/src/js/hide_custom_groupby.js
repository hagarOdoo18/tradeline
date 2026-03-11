/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { _t } from "@web/core/l10n/translation";
import { SearchModel } from "@web/search/search_model";

const LEGACY_TIME_FILTER_PREFIXES = [
    "tradeline_time_based_on_",
    "tradeline_time_compare_",
];
const LEGACY_TIME_FILTER_NAMES = new Set([
    "tradeline_time_based_on_invoice_report",
    "tradeline_time_compare_invoice_report",
    "tradeline_time_based_on_move_line",
    "tradeline_time_compare_move_line",
]);

function isLegacyTimeFilter(item) {
    const name = item?.name || "";
    if (!name) {
        return false;
    }
    if (LEGACY_TIME_FILTER_NAMES.has(name)) {
        return true;
    }
    if (name.startsWith("tradeline_time_ranges_") && item?.type !== "dateFilter") {
        return true;
    }
    return LEGACY_TIME_FILTER_PREFIXES.some((prefix) => name.startsWith(prefix));
}

function isNativeTradelineDateFilter(item) {
    return item?.type === "dateFilter" && (item?.name || "").startsWith("tradeline_time_ranges_");
}

patch(SearchModel.prototype, {
    async load(config) {
        await super.load(...arguments);
        if (config?.context?.tradeline_groupby_expanded) {
            this.hideCustomGroupBy = true;
        }
        if (!config?.context?.tradeline_time_ranges_native) {
            return;
        }

        const searchItems = Array.isArray(this.searchItems) ? this.searchItems : [];
        let filteredItems = searchItems.filter((item) => !isLegacyTimeFilter(item));

        const preferredDateFilterIds = new Set(
            filteredItems.filter(isNativeTradelineDateFilter).map((item) => item.id)
        );
        if (preferredDateFilterIds.size) {
            filteredItems = filteredItems.filter((item) => {
                if (item?.type !== "dateFilter") {
                    return true;
                }
                return preferredDateFilterIds.has(item.id);
            });
        }

        for (const item of filteredItems) {
            if (item?.type === "dateFilter") {
                item.description = _t("Time Ranges");
            }
        }
        this.searchItems = filteredItems;
    },
});

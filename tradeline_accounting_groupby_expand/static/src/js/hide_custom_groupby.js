/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
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

function getSearchItemsArray(searchModel) {
    if (!searchModel || !searchModel.searchItems) {
        return [];
    }
    if (Array.isArray(searchModel.searchItems)) {
        return searchModel.searchItems;
    }
    return Object.values(searchModel.searchItems);
}

function setSearchItemsArray(searchModel, items) {
    const asObject = {};
    for (const item of items) {
        if (item?.id) {
            asObject[item.id] = item;
        }
    }
    searchModel.searchItems = asObject;
}

function cleanLegacyTimeItems(searchModel) {
    const existing = getSearchItemsArray(searchModel);
    const cleaned = existing.filter((item) => !isLegacyTimeFilter(item));
    if (cleaned.length === existing.length) {
        return;
    }
    const cleanedIds = new Set(cleaned.map((item) => item.id));
    searchModel.query = (searchModel.query || []).filter((queryElem) =>
        cleanedIds.has(queryElem.searchItemId)
    );
    setSearchItemsArray(searchModel, cleaned);
}

patch(SearchModel.prototype, {
    async load(config) {
        await super.load(...arguments);
        if (config?.context?.tradeline_groupby_expanded) {
            this.hideCustomGroupBy = true;
        }
        const context = config?.context || {};
        this.tradelineTimeRangesNative = Boolean(context.tradeline_time_ranges_native);
        this.tradelineTimeRangesUIV2 = Boolean(context.tradeline_time_ranges_ui_v2);

        if (!this.tradelineTimeRangesNative && !this.tradelineTimeRangesUIV2) {
            return;
        }

        cleanLegacyTimeItems(this);

        if (this.tradelineTimeRangesUIV2 && this.searchMenuTypes instanceof Set) {
            this.searchMenuTypes.add("comparison");
        }

        const dateFilterIds = getSearchItemsArray(this)
            .filter((item) => item?.type === "dateFilter")
            .map((item) => item.id);
        this.tradelineTimeRangeDateFilterIds = dateFilterIds;
    },
});

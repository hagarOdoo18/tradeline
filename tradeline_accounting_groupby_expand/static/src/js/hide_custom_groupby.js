/** @odoo-module **/

import { Domain } from "@web/core/domain";
import { serializeDate, serializeDateTime } from "@web/core/l10n/dates";
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
const SUPPORTED_CUSTOM_RANGE_KEYS = [
    "last_7_days",
    "last_30_days",
    "last_365_days",
    "today",
    "this_week",
    "this_month",
    "this_quarter",
    "this_year",
    "yesterday",
    "last_week",
    "last_month",
    "last_quarter",
    "last_year",
];

function getActiveComparisonSearchItem(searchModel) {
    for (const queryElem of (searchModel?.query || []).slice().reverse()) {
        const item = searchModel?.searchItems?.[queryElem.searchItemId];
        if (!item) {
            continue;
        }
        if (item.type === "comparison" || (item.type === "favorite" && item.comparison)) {
            return item;
        }
    }
    return null;
}

function extractCustomRangeKey(generatorId) {
    const value = String(generatorId || "");
    if (!value.startsWith("custom_")) {
        return null;
    }
    for (const key of SUPPORTED_CUSTOM_RANGE_KEYS) {
        if (value.endsWith(`_${key}`) || value === `custom_${key}`) {
            return key;
        }
    }
    return null;
}

function computeCustomRangeInterval(referenceMoment, rangeKey) {
    if (!referenceMoment || !rangeKey) {
        return null;
    }
    const today = referenceMoment.startOf("day");
    switch (rangeKey) {
        case "last_7_days":
            return { start: today.minus({ days: 6 }), end: today };
        case "last_30_days":
            return { start: today.minus({ days: 29 }), end: today };
        case "last_365_days":
            return { start: today.minus({ days: 364 }), end: today };
        case "today":
            return { start: today, end: today };
        case "this_week":
            return { start: today.minus({ days: today.weekday - 1 }), end: today };
        case "this_month":
            return { start: today.startOf("month"), end: today };
        case "this_quarter":
            return { start: today.startOf("quarter"), end: today };
        case "this_year":
            return { start: today.startOf("year"), end: today };
        case "yesterday": {
            const yesterday = today.minus({ days: 1 });
            return { start: yesterday, end: yesterday };
        }
        case "last_week": {
            const thisWeekStart = today.minus({ days: today.weekday - 1 });
            return {
                start: thisWeekStart.minus({ days: 7 }),
                end: thisWeekStart.minus({ days: 1 }),
            };
        }
        case "last_month": {
            const thisMonthStart = today.startOf("month");
            return {
                start: thisMonthStart.minus({ months: 1 }),
                end: thisMonthStart.minus({ days: 1 }),
            };
        }
        case "last_quarter": {
            const thisQuarterStart = today.startOf("quarter");
            return {
                start: thisQuarterStart.minus({ months: 3 }),
                end: thisQuarterStart.minus({ days: 1 }),
            };
        }
        case "last_year":
            return {
                start: today.minus({ years: 1 }).startOf("year"),
                end: today.minus({ years: 1 }).endOf("year").startOf("day"),
            };
        default:
            return null;
    }
}

function computeComparisonInterval(interval, comparisonOptionId) {
    if (!interval || !comparisonOptionId) {
        return null;
    }
    if (comparisonOptionId === "previous_year") {
        return {
            start: interval.start.minus({ years: 1 }),
            end: interval.end.minus({ years: 1 }),
        };
    }
    if (comparisonOptionId === "previous_period") {
        const days = Math.max(
            Math.round(interval.end.diff(interval.start, "days").days) + 1,
            1
        );
        return {
            start: interval.start.minus({ days }),
            end: interval.end.minus({ days }),
        };
    }
    return null;
}

function buildIntervalDomain(fieldName, fieldType, interval) {
    if (!fieldName || !interval) {
        return [];
    }
    if (fieldType === "datetime") {
        return [
            [fieldName, ">=", serializeDateTime(interval.start.startOf("day"))],
            [fieldName, "<=", serializeDateTime(interval.end.endOf("day"))],
        ];
    }
    return [
        [fieldName, ">=", serializeDate(interval.start)],
        [fieldName, "<=", serializeDate(interval.end)],
    ];
}

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
    getFullComparison() {
        const activeSearchItem = getActiveComparisonSearchItem(this);
        if (!activeSearchItem || activeSearchItem.type !== "comparison") {
            return super.getFullComparison(...arguments);
        }

        const { dateFilterId, comparisonOptionId } = activeSearchItem;
        const dateFilter = this.searchItems?.[dateFilterId];
        if (!dateFilter || dateFilter.type !== "dateFilter") {
            return super.getFullComparison(...arguments);
        }

        const selectedGeneratorIds = this._getSelectedGeneratorIds(dateFilterId);
        const customGeneratorId = selectedGeneratorIds.find((id) =>
            String(id).startsWith("custom_")
        );
        const customRangeKey = extractCustomRangeKey(customGeneratorId);
        if (!customRangeKey) {
            return super.getFullComparison(...arguments);
        }

        const currentInterval = computeCustomRangeInterval(this.referenceMoment, customRangeKey);
        const previousInterval = computeComparisonInterval(currentInterval, comparisonOptionId);
        if (!currentInterval || !previousInterval) {
            return super.getFullComparison(...arguments);
        }

        const fieldName = dateFilter.fieldName;
        const fieldType = dateFilter.fieldType;
        const baseDomain = dateFilter.domain || [];

        const currentDomain = Domain.and([
            buildIntervalDomain(fieldName, fieldType, currentInterval),
            baseDomain,
        ]);
        const comparisonDomain = Domain.and([
            buildIntervalDomain(fieldName, fieldType, previousInterval),
            baseDomain,
        ]);

        const selectedCustomOption = (dateFilter.optionsParams?.customOptions || []).find(
            (option) => option.id === customGeneratorId
        );
        const rangeDescription =
            selectedCustomOption?.description || dateFilter.description || customRangeKey;
        const comparisonLabel =
            comparisonOptionId === "previous_year" ? "Previous Year" : "Previous Period";

        return {
            comparisonId: comparisonOptionId,
            fieldName,
            fieldDescription: dateFilter.description,
            range: currentDomain.toList(),
            rangeDescription,
            comparisonRange: comparisonDomain.toList(),
            comparisonRangeDescription: `${rangeDescription}: ${comparisonLabel}`,
        };
    },

    async load(config) {
        await super.load(...arguments);
        if (config?.context?.tradeline_groupby_expanded) {
            this.hideCustomGroupBy = true;
        }
        const forcedUiModels = new Set(["account.invoice.report", "account.move.line"]);
        const forceUiByModel = forcedUiModels.has(config?.resModel);
        const context = config?.context || {};
        this.tradelineTimeRangesNative = Boolean(
            context.tradeline_time_ranges_native || forceUiByModel
        );
        this.tradelineTimeRangesUIV2 = Boolean(
            context.tradeline_time_ranges_ui_v2 ||
                context.tradeline_time_ranges_native ||
                forceUiByModel
        );

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

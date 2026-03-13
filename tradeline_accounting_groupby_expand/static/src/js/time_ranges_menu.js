/** @odoo-module **/

import { Component, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";
import { useBus } from "@web/core/utils/hooks";
import { Dropdown } from "@web/core/dropdown/dropdown";
import { useDropdownState } from "@web/core/dropdown/dropdown_hooks";
import { SearchBar } from "@web/search/search_bar/search_bar";
import { SearchBarMenu } from "@web/search/search_bar_menu/search_bar_menu";

const RANGE_SUFFIX_ORDER = [
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

const COMPARISON_OPTION_ORDER = ["previous_period", "previous_year"];

function isTimeRangesUiEnabled(searchModel) {
    return Boolean(searchModel?.tradelineTimeRangesUIV2);
}

function getDateFilters(searchModel) {
    if (!searchModel || typeof searchModel.getSearchItems !== "function") {
        return [];
    }
    return searchModel.getSearchItems((item) => item.type === "dateFilter");
}

function getRawSearchItems(searchModel) {
    return Object.values(searchModel?.searchItems || {});
}

function findActiveComparison(searchModel) {
    for (const queryElem of searchModel?.query || []) {
        const searchItem = searchModel?.searchItems?.[queryElem.searchItemId];
        if (searchItem?.type === "comparison") {
            return searchItem;
        }
    }
    return null;
}

function rankByList(value, ranks) {
    const rank = ranks.indexOf(value);
    return rank >= 0 ? rank : ranks.length + 100;
}

function getRangeRank(optionId) {
    for (let i = 0; i < RANGE_SUFFIX_ORDER.length; i++) {
        const suffix = RANGE_SUFFIX_ORDER[i];
        if (optionId === suffix || optionId.endsWith(`_${suffix}`)) {
            return i;
        }
    }
    return RANGE_SUFFIX_ORDER.length + 100;
}

function getActiveRangeOption(dateFilter) {
    const activeOptions = (dateFilter?.options || []).filter((option) => option.isActive);
    if (!activeOptions.length) {
        return null;
    }
    const nonYearOption = activeOptions.find((option) => !String(option.id).startsWith("year"));
    return nonYearOption || activeOptions[0];
}

function getDefaultRangeOption(dateFilter) {
    const options = dateFilter?.options || [];
    if (!options.length) {
        return null;
    }
    const sortedOptions = [...options].sort((left, right) => {
        const leftRank = getRangeRank(String(left.id));
        const rightRank = getRangeRank(String(right.id));
        if (leftRank !== rightRank) {
            return leftRank - rightRank;
        }
        return String(left.description || "").localeCompare(String(right.description || ""));
    });
    return sortedOptions[0];
}

function normalizeComparisonDescription(item, dateFilterDescription) {
    const description = String(item?.description || "");
    const prefix = `${dateFilterDescription}: `;
    if (description.startsWith(prefix)) {
        return description.slice(prefix.length);
    }
    return description;
}

function parseIntegerId(value) {
    const parsed = Number(value);
    return Number.isInteger(parsed) ? parsed : null;
}

export class TradelineTimeRangesPanel extends Component {
    static template = "tradeline_accounting_groupby_expand.TimeRangesPanel";
    static props = {
        mode: { type: String, optional: true },
        onApplied: { type: Function, optional: true },
    };
    static defaultProps = {
        mode: "inside",
    };

    setup() {
        this.state = useState({
            basedOnId: null,
            rangeId: null,
            compareEnabled: false,
            compareMode: COMPARISON_OPTION_ORDER[0],
        });
        useBus(this.env.searchModel, "update", () => this.syncFromModel());
        this.syncFromModel();
    }

    get searchModel() {
        return this.env.searchModel;
    }

    get isEnabled() {
        return isTimeRangesUiEnabled(this.searchModel);
    }

    get dateFilters() {
        return getDateFilters(this.searchModel);
    }

    get selectedDateFilter() {
        return this.dateFilters.find((item) => item.id === this.state.basedOnId) || null;
    }

    get rangeOptions() {
        const dateFilter = this.selectedDateFilter;
        if (!dateFilter) {
            return [];
        }
        return [...(dateFilter.options || [])].sort((left, right) => {
            const leftRank = getRangeRank(String(left.id));
            const rightRank = getRangeRank(String(right.id));
            if (leftRank !== rightRank) {
                return leftRank - rightRank;
            }
            return String(left.description || "").localeCompare(String(right.description || ""));
        });
    }

    get isCustomRange() {
        return String(this.state.rangeId || "").startsWith("custom_");
    }

    get comparisonItems() {
        const selectedDateFilter = this.selectedDateFilter;
        if (!selectedDateFilter) {
            return [];
        }
        const rawItems = getRawSearchItems(this.searchModel)
            .filter(
                (item) =>
                    item.type === "comparison" && item.dateFilterId === selectedDateFilter.id
            )
            .sort((left, right) => {
                const leftRank = rankByList(left.comparisonOptionId, COMPARISON_OPTION_ORDER);
                const rightRank = rankByList(right.comparisonOptionId, COMPARISON_OPTION_ORDER);
                if (leftRank !== rightRank) {
                    return leftRank - rightRank;
                }
                return String(left.description || "").localeCompare(String(right.description || ""));
            });
        return rawItems.map((item) => ({
            ...item,
            shortDescription: normalizeComparisonDescription(item, selectedDateFilter.description),
        }));
    }

    get canCompare() {
        return Boolean(this.state.rangeId) && !this.isCustomRange && this.comparisonItems.length > 0;
    }

    get canApply() {
        return Boolean(this.state.basedOnId && this.state.rangeId);
    }

    syncFromModel() {
        if (!this.isEnabled) {
            this.state.basedOnId = null;
            this.state.rangeId = null;
            this.state.compareEnabled = false;
            this.state.compareMode = COMPARISON_OPTION_ORDER[0];
            return;
        }

        const dateFilters = this.dateFilters;
        if (!dateFilters.length) {
            this.state.basedOnId = null;
            this.state.rangeId = null;
            this.state.compareEnabled = false;
            this.state.compareMode = COMPARISON_OPTION_ORDER[0];
            return;
        }

        const activeDateFilter = dateFilters.find((item) => item.isActive) || dateFilters[0];
        const selectedDateFilter =
            dateFilters.find((item) => item.id === this.state.basedOnId) || activeDateFilter;
        this.state.basedOnId = selectedDateFilter.id;

        const activeRangeOption = getActiveRangeOption(selectedDateFilter);
        const defaultRangeOption = getDefaultRangeOption(selectedDateFilter);
        const availableRangeIds = new Set((selectedDateFilter.options || []).map((o) => o.id));
        if (
            !this.state.rangeId ||
            !availableRangeIds.has(this.state.rangeId) ||
            activeRangeOption
        ) {
            this.state.rangeId = (activeRangeOption || defaultRangeOption)?.id || null;
        }

        const activeComparison = findActiveComparison(this.searchModel);
        if (
            activeComparison &&
            activeComparison.dateFilterId === selectedDateFilter.id &&
            !String(this.state.rangeId || "").startsWith("custom_")
        ) {
            this.state.compareEnabled = true;
            this.state.compareMode =
                activeComparison.comparisonOptionId || COMPARISON_OPTION_ORDER[0];
        } else {
            this.state.compareEnabled = false;
            if (!COMPARISON_OPTION_ORDER.includes(this.state.compareMode)) {
                this.state.compareMode = COMPARISON_OPTION_ORDER[0];
            }
        }
    }

    onBasedOnChange(ev) {
        const basedOnId = parseIntegerId(ev.target.value);
        this.state.basedOnId = basedOnId;
        const dateFilter = this.dateFilters.find((item) => item.id === basedOnId);
        const activeRangeOption = getActiveRangeOption(dateFilter);
        const defaultRangeOption = getDefaultRangeOption(dateFilter);
        this.state.rangeId = (activeRangeOption || defaultRangeOption)?.id || null;
        if (String(this.state.rangeId || "").startsWith("custom_")) {
            this.state.compareEnabled = false;
        }
    }

    onRangeChange(ev) {
        this.state.rangeId = ev.target.value || null;
        if (this.isCustomRange) {
            this.state.compareEnabled = false;
        }
    }

    onCompareToggle(ev) {
        this.state.compareEnabled = Boolean(ev.target.checked) && this.canCompare;
    }

    onCompareModeChange(ev) {
        this.state.compareMode = ev.target.value || COMPARISON_OPTION_ORDER[0];
    }

    clearActiveDateFilters() {
        for (const dateFilter of getDateFilters(this.searchModel)) {
            const activeOptions = (dateFilter.options || []).filter((option) => option.isActive);
            for (const option of activeOptions) {
                this.searchModel.toggleDateFilter(dateFilter.id, option.id);
            }
        }
    }

    clearActiveComparisons() {
        for (const queryElem of [...(this.searchModel.query || [])]) {
            const searchItem = this.searchModel.searchItems?.[queryElem.searchItemId];
            if (searchItem?.type === "comparison") {
                this.searchModel.toggleSearchItem(searchItem.id);
            }
        }
    }

    applySelection() {
        if (!this.canApply) {
            return;
        }

        const selectedDateFilterId = this.state.basedOnId;
        const selectedRangeId = this.state.rangeId;
        this.clearActiveComparisons();
        this.clearActiveDateFilters();
        this.searchModel.toggleDateFilter(selectedDateFilterId, selectedRangeId);

        if (this.state.compareEnabled && this.canCompare) {
            const selectedComparison =
                this.comparisonItems.find(
                    (item) => item.comparisonOptionId === this.state.compareMode
                ) || this.comparisonItems[0];
            if (selectedComparison) {
                this.searchModel.toggleSearchItem(selectedComparison.id);
            }
        }

        if (this.props.onApplied) {
            this.props.onApplied();
        }
    }
}

export class TradelineTimeRangesShortcut extends Component {
    static template = "tradeline_accounting_groupby_expand.TimeRangesShortcut";
    static components = {
        Dropdown,
        TradelineTimeRangesPanel,
    };

    setup() {
        this.dropdownState = useDropdownState();
        useBus(this.env.searchModel, "update", this.render);
    }

    get isVisible() {
        return isTimeRangesUiEnabled(this.env.searchModel) && getDateFilters(this.env.searchModel).length > 0;
    }

    onApplied() {
        if (this.dropdownState?.close) {
            this.dropdownState.close();
        }
    }

    get buttonLabel() {
        return _t("Time Ranges");
    }
}

patch(SearchBarMenu, {
    components: {
        ...SearchBarMenu.components,
        TradelineTimeRangesPanel,
    },
});

patch(SearchBar, {
    components: {
        ...SearchBar.components,
        TradelineTimeRangesShortcut,
    },
});

patch(SearchBarMenu.prototype, {
    get filterItems() {
        const items = this.env.searchModel.getSearchItems((searchItem) =>
            ["filter", "dateFilter"].includes(searchItem.type)
        );
        if (!isTimeRangesUiEnabled(this.env.searchModel)) {
            return items;
        }
        return items.filter((item) => item.type !== "dateFilter");
    },

    get showComparisonMenu() {
        if (isTimeRangesUiEnabled(this.env.searchModel)) {
            return false;
        }
        return (
            this.env.searchModel.searchMenuTypes.has("comparison") &&
            this.env.searchModel.getSearchItems((item) => item.type === "comparison").length > 0
        );
    },

    get showTimeRangesMenu() {
        return (
            isTimeRangesUiEnabled(this.env.searchModel) &&
            this.env.searchModel.searchMenuTypes.has("filter") &&
            getDateFilters(this.env.searchModel).length > 0
        );
    },

    onTimeRangesApplied() {
        if (this.props.dropdownState?.close) {
            this.props.dropdownState.close();
        }
    },
});

patch(SearchBar.prototype, {
    get showTradelineTimeRangesShortcut() {
        return isTimeRangesUiEnabled(this.env.searchModel) && getDateFilters(this.env.searchModel).length > 0;
    },
});

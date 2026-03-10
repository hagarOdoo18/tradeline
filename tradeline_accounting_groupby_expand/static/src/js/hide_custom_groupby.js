/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { _t } from "@web/core/l10n/translation";
import { SearchModel } from "@web/search/search_model";

patch(SearchModel.prototype, {
    async load(config) {
        await super.load(...arguments);
        if (config?.context?.tradeline_groupby_expanded) {
            this.hideCustomGroupBy = true;
        }
        if (config?.context?.tradeline_time_ranges_enabled) {
            const searchItems = Array.isArray(this.searchItems) ? this.searchItems : [];
            const hasExplicitTimeRanges = searchItems.some(
                (item) => item?.name?.startsWith?.("tradeline_time_ranges_")
            );
            if (!hasExplicitTimeRanges) {
                const firstDateFilter = searchItems.find((item) => item?.type === "dateFilter");
                if (firstDateFilter && firstDateFilter.description !== _t("Time Ranges")) {
                    firstDateFilter.description = _t("Time Ranges");
                }
            }
        }
    },
});

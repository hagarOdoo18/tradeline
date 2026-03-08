/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { SearchModel } from "@web/search/search_model";

patch(SearchModel.prototype, {
    async load(config) {
        await super.load(...arguments);
        if (config?.context?.tradeline_groupby_expanded) {
            this.hideCustomGroupBy = true;
        }
    },
});

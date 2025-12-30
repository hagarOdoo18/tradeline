/** @odoo-module **/

import { EditListPopup } from "@point_of_sale/app/store/select_lot_popup/select_lot_popup";
import { patch } from "@web/core/utils/patch";
import { usePos } from "@point_of_sale/app/store/pos_hook";
import { AlertDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { useService } from "@web/core/utils/hooks";
import { onMounted } from "@odoo/owl";
patch(EditListPopup.prototype, {

    onInput(ev) {
        // call original behavior
        super.onInput(ev);

        const query = ev.target.value?.trim().toLowerCase();
        this.state.selectedOptionValue = null;

        if (!query || query.length < 2) {
            this.displayedOptions = [];
            return;
        }

        this.displayedOptions = this._searchLots(query);
        this.state.hideOptions = false;
    },

    _searchLots(query) {
        const pos = this.env.pos;
        const product = this.props.product;

        if (!product) {
            return [];
        }

        // all cached lots
        const lots = pos.db.lots || [];

        return lots
            .filter(lot =>
                lot.product_id?.[0] === product.id &&
                lot.name.toLowerCase().includes(query)
            )
            .slice(0, 10) // limit results
            .map(lot => lot.name);
    },

    onSelectOption(option) {
        this.props.item.text = option;
        this.displayedOptions = [];
        this.state.hideOptions = true;
    },

});

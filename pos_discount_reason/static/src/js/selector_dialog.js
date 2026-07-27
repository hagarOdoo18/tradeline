/** @odoo-module */

import { Dialog } from "@web/core/dialog/dialog";
import { _t } from "@web/core/l10n/translation";
import { useService } from "@web/core/utils/hooks";
import { Input } from "@point_of_sale/app/generic_components/inputs/input/input";
import { Component, useState } from "@odoo/owl";
import { unaccent } from "@web/core/utils/strings";

export class PosChoiceListDialog extends Component {
    static template = "pos_discount_reason.PosChoiceListDialog";
    static components = { Dialog, Input };
    static props = {
        title: String,
        items: Array,
        selectedId: { optional: true },
        getPayload: Function,
        close: Function,
        placeholder: { type: String, optional: true },
        emptyMessage: { type: String, optional: true },
    };

    setup() {
        this.ui = useState(useService("ui"));
        this.state = useState({
            query: "",
        });
    }

    get filteredItems() {
        const query = unaccent((this.state.query || "").trim().toLowerCase(), false);
        const items = this.props.items || [];
        if (!query) {
            return items;
        }
        return items.filter((item) =>
            unaccent((item.label || "").toLowerCase(), false).includes(query)
        );
    }

    isSelected(item) {
        return item?.id === this.props.selectedId;
    }

    selectItem(item) {
        this.props.getPayload(item?.item || item);
        this.props.close();
    }

    unselect() {
        this.props.getPayload(null);
        this.props.close();
    }

    discard() {
        this.props.close();
    }

    get placeholder() {
        return this.props.placeholder || _t("Search...");
    }

    get emptyMessage() {
        return this.props.emptyMessage || _t("No records found.");
    }
}

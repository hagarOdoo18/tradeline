/** @odoo-module **/
import { ControlButtons } from "@point_of_sale/app/screens/product_screen/control_buttons/control_buttons";
import { Component } from "@odoo/owl";
import { usePos } from "@point_of_sale/app/store/pos_hook";
import { _t } from "@web/core/l10n/translation";
import { SelectionPopup } from "@point_of_sale/app/utils/input_popups/selection_popup";
import { patch } from "@web/core/utils/patch";
import { makeAwaitable } from "@point_of_sale/app/store/make_awaitable_dialog";
import { useService } from "@web/core/utils/hooks";
import { AlertDialog } from "@web/core/confirmation_dialog/confirmation_dialog";


patch(ControlButtons.prototype, {
    setup() {
     super.setup()
        this.orm = useService("orm");
    },
    async onClick() {
     const orderline = this.pos.get_order().get_selected_orderline();
     if (!orderline) {
     this.dialog.add(AlertDialog, {
                        title: _t("No orderline selected."),
                        body: _t("Select orderline to add salesperson"),
                    });
    return;
    }
     this.res_users = await this.orm.call("res.users", "search_read", [[], ['id', 'name']]);
     const salespersonList = this.res_users.map((salesperson) => ({
        id: salesperson.id,
        item: salesperson,
        label: salesperson.name,
        isSelected: false,
     }));
    const confirmed = await makeAwaitable(this.dialog, SelectionPopup, {
        title: _t("Select the Salesperson"),
        list: salespersonList,

    });
    if (confirmed) {
     this.orm.write("product.product", [orderline.product_id.id], {
                  pos_sales_person_id : confirmed.id
                });
     orderline.salesperson = confirmed.name;
     orderline.user_id = confirmed.id;
    }
    }
});

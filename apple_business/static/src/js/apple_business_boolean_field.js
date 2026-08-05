/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import { ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import {
    BooleanField,
    booleanField,
} from "@web/views/fields/boolean/boolean_field";

export class AppleBusinessBooleanField extends BooleanField {
    setup() {
        super.setup();
        this.action = useService("action");
        this.dialog = useService("dialog");
        this.notification = useService("notification");
        this.orm = useService("orm");
    }

    getRelationId(value) {
        if (Array.isArray(value)) {
            return value[0];
        }
        if (value && typeof value === "object") {
            return value.id || value.resId || false;
        }
        return value || false;
    }

    getRelationName(value, fallback) {
        if (Array.isArray(value)) {
            return value[1] || fallback;
        }
        if (value && typeof value === "object") {
            return value.display_name || value.name || fallback;
        }
        return fallback;
    }

    async resetUnchecked() {
        this.state.value = true;
        await this.render();
        this.state.value = false;
        await this.props.record.update({ [this.props.name]: false });
        await this.render();
    }

    async onChange(newValue) {
        if (!newValue) {
            return super.onChange(false);
        }

        const partnerId = this.getRelationId(this.props.record.data.partner_id);
        const branchId = this.getRelationId(this.props.record.data.branch_id);
        if (!partnerId || !branchId) {
            this.notification.add(
                _t("Select a company customer and branch first."),
                { type: "warning" }
            );
            return;
        }

        try {
            const status = await this.orm.call(
                "sale.order",
                "get_apple_business_subscription_status",
                [partnerId, branchId]
            );
            if (!status.eligible) {
                await this.resetUnchecked();
                this.notification.add(
                    _t("Apple Business is only available for company customers."),
                    { type: "warning" }
                );
                return;
            }
            if (status.subscription_id) {
                return super.onChange(true);
            }

            return super.onChange(true);
        } catch (error) {
            await this.resetUnchecked();
            this.notification.add(
                error?.message || _t("Could not check the Apple Business subscription."),
                { type: "danger" }
            );
        }
    }
}

export const appleBusinessBooleanField = {
    ...booleanField,
    component: AppleBusinessBooleanField,
};

registry.category("fields").add(
    "apple_business_boolean",
    appleBusinessBooleanField
);

registry.category("actions").add(
    "apple_business_subscription_prompt",
    (env, action) => {
        const params = action.params || {};
        env.services.dialog.add(ConfirmationDialog, {
            title: _t("Apple Business Subscription Required"),
            body: _t(
                "Invoice %s is finished. Create the Apple Business subscription now? This invoice will be suggested, and you can select another posted invoice for the same company and branch.",
                params.invoice_name
            ),
            confirmLabel: _t("Create Subscription"),
            cancelLabel: _t("Not Now"),
            cancel: () => {},
            confirm: () => {
                env.services.action.doAction({
                    type: "ir.actions.act_window",
                    name: _t("New Apple Business Subscription"),
                    res_model: "apple.business",
                    views: [[false, "form"]],
                    target: "new",
                    context: {
                        default_partner_id: params.partner_id,
                        default_branch_id: params.branch_id,
                        default_invoice_id: params.invoice_id,
                    },
                });
            },
        });
    }
);

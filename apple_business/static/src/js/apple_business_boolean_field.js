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

            await this.resetUnchecked();
            const partnerName =
                status.partner_name ||
                this.getRelationName(
                    this.props.record.data.partner_id,
                    _t("This company")
                );
            const branchName =
                status.branch_name ||
                this.getRelationName(
                    this.props.record.data.branch_id,
                    _t("the selected branch")
                );
            if (!status.suggested_invoice_id) {
                this.dialog.add(ConfirmationDialog, {
                    title: _t("Sales Invoice Required"),
                    body: _t(
                        "Create and post the sales invoice normally for %s at %s. Then select Apple Business again to create the subscription.",
                        partnerName,
                        branchName
                    ),
                    confirmLabel: _t("OK"),
                    cancelLabel: _t("Not Now"),
                    cancel: () => {},
                    confirm: () => {},
                });
                return;
            }

            this.dialog.add(ConfirmationDialog, {
                title: _t("Apple Business Subscription Required"),
                body: _t(
                    "%s needs a confirmed Apple Business subscription for %s. Invoice %s will be suggested, and you can select another posted invoice for the same company and branch.",
                    partnerName,
                    branchName,
                    status.suggested_invoice_name
                ),
                confirmLabel: _t("Create Subscription"),
                cancelLabel: _t("Not Now"),
                cancel: () => {},
                confirm: () => {
                    this.action.doAction({
                        type: "ir.actions.act_window",
                        name: _t("New Apple Business Subscription"),
                        res_model: "apple.business",
                        views: [[false, "form"]],
                        target: "new",
                        context: {
                            default_partner_id: partnerId,
                            default_branch_id: branchId,
                            default_invoice_id: status.suggested_invoice_id,
                        },
                    });
                },
            });
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

/** @odoo-module **/
import { registry } from "@web/core/registry";
import { _t } from "@web/core/l10n/translation";

registry.category("actions").add("tradeline_eta_staff_operation", async (env, action) => {
    const { invoice_id, request_id, mode } = action.params;
    const remove = env.services.notification.add(
        mode === "send"
            ? _t("Checking and sending this invoice to ETA now.")
            : _t("Checking this invoice with ETA. This action does not send an invoice."),
        { title: mode === "send" ? _t("Send to ETA now") : _t("Check ETA status"), sticky: true, type: "info" }
    );
    try {
        const result = await env.services.orm.call("account.move", "action_eta_execute_staff_request",
            [[invoice_id], request_id]);
        env.services.notification.add(result.message, {
            title: result.title, type: result.type, sticky: true,
        });
        return { type: "ir.actions.client", tag: "reload" };
    } finally {
        remove();
    }
});

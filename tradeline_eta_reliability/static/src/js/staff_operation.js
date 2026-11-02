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
        // Refresh the current list/form without throwing away the result toast
        // or the accountant's filters and grouping in a full browser reload.
        return { type: "ir.actions.client", tag: "soft_reload" };
    } finally {
        remove();
    }
});

registry.category("actions").add("tradeline_eta_local_vat_result", (env, action) => {
    const params = action.params;
    const buttons = params.blocked_ids?.length ? [{
        name: params.blocked_button_name || _t("Open blocked invoices"),
        onClick: () => env.services.action.doAction(params.blocked_action),
    }] : [];
    env.services.notification.add(params.message, {
        title: params.title, type: params.type, sticky: true, buttons,
    });
    // Keep the accountant's filters while removing completed rows from the
    // actionable VAT list. A result notification is the only intermediate UI.
    return { type: "ir.actions.client", tag: "soft_reload" };
});

registry.category("actions").add("tradeline_eta_local_vat_batch", async (env, action) => {
    const ids = action.params.move_ids;
    const batchSize = Math.max(1, Math.min(action.params.batch_size || 25, 25));
    let applied = 0, skipped = 0, processed = 0, unconfirmed = [];
    const blocked = [];
    let removeProgress;
    try {
        for (let offset = 0; offset < ids.length; offset += batchSize) {
            removeProgress?.();
            removeProgress = env.services.notification.add(
                _t("Applying 14% VAT in Odoo: %s of %s invoices checked.", processed, ids.length),
                { title: _t("Applying VAT — Odoo only"), sticky: true, type: "info" }
            );
            const result = await env.services.orm.call("account.move", "action_eta_apply_local_vat_batch",
                [ids.slice(offset, offset + batchSize)]);
            applied += result.params.applied_count;
            skipped += result.params.skipped_count;
            blocked.push(...result.params.blocked_items);
            processed += Math.min(batchSize, ids.length - offset);
        }
    } catch {
        // A connection failure can occur after a transaction committed. Only
        // acknowledged batches count as applied; refreshing makes retry safe.
        unconfirmed = ids.slice(processed);
    } finally {
        removeProgress?.();
    }
    let message = _t("Applied: %s. Already has VAT / skipped: %s. Blocked: %s.", applied, skipped, blocked.length);
    if (blocked.length) {
        message += "\n" + blocked.slice(0, 5).map(item => `${item.invoice}: ${item.reason}`).join("\n");
    }
    if (unconfirmed.length) {
        message += "\n" + _t("Not confirmed: %s. Processing stopped. The last batch may have completed. Refresh and retry invoices still missing VAT.", unconfirmed.length);
    }
    const blockedIds = [...blocked.map(item => item.id), ...unconfirmed];
    return {
        type: "ir.actions.client", tag: "tradeline_eta_local_vat_result",
        params: {
            title: _t("14% VAT in Odoo only"), message,
            type: blockedIds.length ? "warning" : "success", blocked_ids: blockedIds,
            blocked_button_name: unconfirmed.length ? _t("Open invoices needing attention") : undefined,
            blocked_action: {
                type: "ir.actions.act_window", name: _t("VAT invoices needing attention"),
                res_model: "account.move", view_mode: "list,form", views: [[false, "list"], [false, "form"]],
                domain: [["id", "in", blockedIds]],
            },
        },
    };
});

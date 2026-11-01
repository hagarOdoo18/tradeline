/** @odoo-module **/
import { Component, onWillStart, onMounted, onWillUnmount, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { listView } from "@web/views/list/list_view";
import { ListController } from "@web/views/list/list_controller";
import { SearchBar } from "@web/search/search_bar/search_bar";
import { _t } from "@web/core/l10n/translation";

export class EtaConnectionHealth extends Component {
    static template = "tradeline_eta_reliability.ConnectionHealth";
    static props = { "*": true };
    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = useState({ rows: [], failed: false, query: "", finding: false });
        this.alive = true;
        onWillStart(() => this.refresh());
        onMounted(() => { this.timer = setInterval(() => this.refresh(), 30000); });
        onWillUnmount(() => { this.alive = false; clearInterval(this.timer); });
    }
    async refresh() {
        if (this.loading) { return; }
        this.loading = true;
        try {
            const rows = await this.orm.call("tradeline.eta.job", "get_staff_connection_status", []);
            if (this.alive) { this.state.rows = rows; this.state.failed = false; }
        } catch {
            if (this.alive) { this.state.failed = true; }
        } finally { this.loading = false; }
    }
    label(state) {
        return ({ ok: _t("ETA available"), slow: _t("ETA responding slowly"), connection: _t("ETA connection issue"),
            access: _t("ETA login / access issue"), waiting: _t("ETA requested a wait"), paused: _t("Processing paused"),
            disabled: _t("ETA transport disabled in this database"), stale: _t("ETA check is stale"),
            unchecked: _t("ETA connection not checked yet") })[state] || state;
    }
    color(state) {
        return state === "ok" ? "text-bg-success" : ["connection", "access"].includes(state) ? "text-bg-danger" : "text-bg-warning";
    }
    duration(value) {
        if (value === false || value === undefined || value === null) { return _t("Collecting samples"); }
        return value >= 60 ? _t("%s min", (value / 60).toFixed(1)) : _t("%s sec", Number(value).toFixed(2));
    }
    checked(value) { return value ? new Date(value.replace(" ", "T") + "Z").toLocaleTimeString() : "—"; }
    vatLane() { return this.props.context?.eta_vat_lane || ""; }
    vatInstructions() {
        return ({
            all: _t("Choose a VAT work list below. Reading this page creates no credit notes. Select one invoice and confirm Prepare VAT Correction to save correction drafts."),
            unsent: _t("No ETA submission is recorded. Add VAT to the same invoice, review the total, confirm/post, then send. No credit note is needed."),
            approved: _t("These originals are already approved by ETA without VAT. Prepare a credit and VAT replacement, review the drafts, then send the credit first. Send the replacement only after ETA approves the credit."),
            in_progress: _t("Correction documents already exist. Open existing corrections and follow the invoice's instructions. Do not create a second credit note. History includes cancelled or superseded drafts."),
            rejected: _t("ETA marked these invoices Invalid. Correct VAT verifies that rejection before reopening the original. A confirmed validation rejection does not require a credit note."),
            review: _t("The existing delivery or receipt needs reconciliation. Do not treat an unconfirmed submission as unsent, or create a credit before its outcome is confirmed.")
        })[this.vatLane()] || "";
    }
    async findInvoice() {
        if (!this.state.query.trim() || this.state.finding) { return; }
        this.state.finding = true;
        try {
            const result = await this.orm.call("account.move", "action_eta_find_invoice", [this.state.query]);
            await this.action.doAction(result);
        } finally { this.state.finding = false; }
    }
    open(suffix) { return this.action.doAction("tradeline_eta_reliability.action_eta_" + suffix); }
}
class EtaWorkspaceFilterBar extends SearchBar {
    static template = "tradeline_eta_reliability.WorkspaceFilterBar";
}
class EtaWorkspaceListController extends ListController {
    static template = "tradeline_eta_reliability.WorkspaceList";
    static components = { ...ListController.components, SearchBar: EtaWorkspaceFilterBar, EtaConnectionHealth };
}
registry.category("views").add("eta_workspace_list", { ...listView, Controller: EtaWorkspaceListController });
registry.category("view_widgets").add("eta_connection_health", { component: EtaConnectionHealth });

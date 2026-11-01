/** @odoo-module **/
import { Component, onWillStart, onMounted, onWillUnmount, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { listView } from "@web/views/list/list_view";
import { ListController } from "@web/views/list/list_controller";
import { _t } from "@web/core/l10n/translation";

export class EtaConnectionHealth extends Component {
    static template = "tradeline_eta_reliability.ConnectionHealth";
    static props = { "*": true };
    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = useState({ rows: [], failed: false });
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
    open(suffix) { return this.action.doAction("tradeline_eta_reliability.action_eta_" + suffix); }
}
class EtaWorkspaceListController extends ListController {
    static template = "tradeline_eta_reliability.WorkspaceList";
    static components = { ...ListController.components, EtaConnectionHealth };
}
registry.category("views").add("eta_workspace_list", { ...listView, Controller: EtaWorkspaceListController });
registry.category("view_widgets").add("eta_connection_health", { component: EtaConnectionHealth });

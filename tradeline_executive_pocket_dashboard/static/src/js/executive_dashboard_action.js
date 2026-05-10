/** @odoo-module **/

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, onWillStart, useState } from "@odoo/owl";

const DRILL_GROUPS = {
    finance: ["branch", "customer", "payment_state"],
    sales: ["branch", "salesperson", "customer"],
    inventory: ["category", "company", "product"],
    pipeline: ["stage", "owner", "branch"],
};

export class ExecutivePocketDashboard extends Component {
    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");

        const today = new Date();
        const start = new Date(today.getFullYear(), today.getMonth(), 1);
        this.state = useState({
            loading: true,
            refreshingFx: false,
            lens: "overview",
            selectedDomain: "finance",
            selectedGroupBy: "branch",
            filters: {
                date_preset: "MTD",
                start_date: this._formatDate(start),
                end_date: this._formatDate(today),
                company_ids: [],
                branch_ids: [],
                salesperson_ids: [],
            },
            bundle: null,
            error: "",
        });

        onWillStart(async () => {
            await this._loadBundle();
        });
    }

    get cards() {
        return this.state.bundle?.cards || [];
    }

    get alerts() {
        return this.state.bundle?.alerts || [];
    }

    get fxCards() {
        return this.state.bundle?.fx_watch?.cards || [];
    }

    get drillRows() {
        return this.state.bundle?.drilldown?.rows || [];
    }

    get drillColumns() {
        return this.state.bundle?.drilldown?.columns || [];
    }

    get availableGroups() {
        return DRILL_GROUPS[this.state.selectedDomain] || ["branch"];
    }

    async _loadBundle() {
        this.state.loading = true;
        this.state.error = "";
        try {
            const bundle = await this.orm.call(
                "tradeline.executive.dashboard.service",
                "get_dashboard_bundle",
                [this.state.filters, this.state.lens, ["overview", this.state.selectedDomain, this.state.selectedGroupBy, "details"]]
            );
            this.state.bundle = bundle;
            await this._reloadDrilldown();
        } catch (error) {
            this.state.error = error?.message || "Failed to load dashboard";
        } finally {
            this.state.loading = false;
        }
    }

    async _reloadDrilldown() {
        try {
            const drilldown = await this.orm.call(
                "tradeline.executive.dashboard.service",
                "get_drilldown",
                [this.state.selectedDomain, "value", this.state.selectedGroupBy, this.state.filters, 30, 0]
            );
            if (this.state.bundle) {
                this.state.bundle.drilldown = drilldown;
            }
        } catch (error) {
            this.notification.add("Failed to load drilldown data", { type: "warning" });
        }
    }

    _formatDate(value) {
        const yyyy = value.getFullYear();
        const mm = String(value.getMonth() + 1).padStart(2, "0");
        const dd = String(value.getDate()).padStart(2, "0");
        return `${yyyy}-${mm}-${dd}`;
    }

    _formatCurrency(value) {
        const num = Number(value || 0);
        return new Intl.NumberFormat("en-EG", {
            style: "currency",
            currency: "EGP",
            maximumFractionDigits: 0,
        }).format(num);
    }

    _formatNumber(value) {
        const num = Number(value || 0);
        return new Intl.NumberFormat("en-EG", { maximumFractionDigits: 2 }).format(num);
    }

    _formatPercent(value) {
        const num = Number(value || 0);
        return `${num.toFixed(2)}%`;
    }

    _formatFxRate(value) {
        const num = Number(value || 0);
        return num.toFixed(6);
    }

    _formatCell(column, value) {
        if (value === null || value === undefined) {
            return "-";
        }
        const text = String(column || "");
        if (text.includes("revenue") || text.includes("value") || text.includes("pipeline")) {
            return this._formatCurrency(value);
        }
        if (text.includes("rate")) {
            return Number(value).toFixed(6);
        }
        if (text.includes("pct") || text.includes("percent")) {
            return this._formatPercent(value);
        }
        if (typeof value === "number") {
            return this._formatNumber(value);
        }
        return value;
    }

    async onLensChange(ev) {
        this.state.lens = ev.target.value;
        await this._loadBundle();
    }

    async onDomainChange(ev) {
        this.state.selectedDomain = ev.target.value;
        const groups = this.availableGroups;
        this.state.selectedGroupBy = groups[0];
        await this._reloadDrilldown();
    }

    async onGroupChange(ev) {
        this.state.selectedGroupBy = ev.target.value;
        await this._reloadDrilldown();
    }

    async onDateChange() {
        await this._loadBundle();
    }

    async onRefreshFx() {
        this.state.refreshingFx = true;
        try {
            await this.orm.call("tradeline.executive.dashboard.service", "refresh_fx_rates", []);
            await this._loadBundle();
            this.notification.add("FX rates refreshed", { type: "success" });
        } catch (error) {
            this.notification.add("FX refresh failed, showing last good rates", { type: "warning" });
            await this._loadBundle();
        } finally {
            this.state.refreshingFx = false;
        }
    }

    async openNativeView(domain) {
        const map = {
            finance: { name: "Invoices", model: "account.move", domain: [["move_type", "in", ["out_invoice", "out_receipt", "out_refund"]]] },
            sales: { name: "Sales Orders", model: "sale.order", domain: [] },
            inventory: { name: "Stock Quants", model: "stock.quant", domain: [] },
            pipeline: { name: "Opportunities", model: "crm.lead", domain: [["type", "=", "opportunity"]] },
        };
        const target = map[domain] || map.finance;
        await this.action.doAction({
            type: "ir.actions.act_window",
            name: target.name,
            res_model: target.model,
            view_mode: "list,form,pivot,graph",
            domain: target.domain,
            target: "current",
        });
    }

    async onOpenNativeView() {
        await this.openNativeView(this.state.selectedDomain);
    }
}

ExecutivePocketDashboard.template = "tradeline_executive_pocket_dashboard.MainDashboard";
registry.category("actions").add("tradeline_executive_pocket_dashboard.main", ExecutivePocketDashboard);

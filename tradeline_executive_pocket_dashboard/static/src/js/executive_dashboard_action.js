/** @odoo-module **/

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, onWillStart, useState } from "@odoo/owl";

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
            selectedMetric: "net_revenue",
            sort: {
                column: "",
                direction: "",
            },
            pagination: {
                limit: 30,
                offset: 0,
            },
            companyPicker: {
                open: false,
                search: "",
                draft_ids: [],
            },
            filters: {
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
        const rows = this.state.bundle?.drilldown?.rows || [];
        const sortCol = this.state.sort.column;
        const sortDir = this.state.sort.direction;
        if (!sortCol || !sortDir) {
            return rows;
        }
        const sorted = [...rows];
        const direction = sortDir === "asc" ? 1 : -1;
        const comparable = sorted
            .map((row) => row?.[sortCol])
            .filter((value) => value !== null && value !== undefined && value !== "");
        if (!comparable.length) {
            return sorted;
        }
        const numeric = comparable.every((value) => typeof value === "number" || (typeof value === "string" && value.trim() !== "" && Number.isFinite(Number(value))));
        sorted.sort((a, b) => {
            const left = a?.[sortCol];
            const right = b?.[sortCol];
            const leftMissing = left === null || left === undefined || left === "";
            const rightMissing = right === null || right === undefined || right === "";
            if (leftMissing && rightMissing) {
                return 0;
            }
            if (leftMissing) {
                return 1;
            }
            if (rightMissing) {
                return -1;
            }
            if (numeric) {
                return (Number(left) - Number(right)) * direction;
            }
            return String(left).localeCompare(String(right), undefined, { sensitivity: "base" }) * direction;
        });
        return sorted;
    }

    get drillColumns() {
        return this.state.bundle?.drilldown?.columns || [];
    }

    get drillHasCompanyColumn() {
        return this.drillColumns.includes("company");
    }

    get companySplitNotice() {
        if (!this.drillHasCompanyColumn) {
            return "";
        }
        return "Company split active (multiple companies selected).";
    }

    get coverage() {
        return this.state.bundle?.coverage || {};
    }

    get drillTotalCount() {
        return Number(this.state.bundle?.drilldown?.total_count || 0);
    }

    get drillLimit() {
        return Number(this.state.pagination.limit || 30);
    }

    get drillOffset() {
        return Number(this.state.pagination.offset || 0);
    }

    get drillPageStart() {
        if (!this.drillTotalCount) {
            return 0;
        }
        return this.drillOffset + 1;
    }

    get drillPageEnd() {
        if (!this.drillTotalCount) {
            return 0;
        }
        return Math.min(this.drillOffset + this.drillLimit, this.drillTotalCount);
    }

    get drillTotalPages() {
        if (!this.drillTotalCount) {
            return 1;
        }
        return Math.max(1, Math.ceil(this.drillTotalCount / this.drillLimit));
    }

    get drillCurrentPage() {
        return Math.floor(this.drillOffset / this.drillLimit) + 1;
    }

    get hasPrevPage() {
        return this.drillOffset > 0;
    }

    get hasNextPage() {
        return this.drillOffset + this.drillLimit < this.drillTotalCount;
    }

    get pageSizeOptions() {
        return [25, 50, 100, 200];
    }

    get drillPageSummary() {
        if (!this.drillTotalCount) {
            return "No grouped rows found";
        }
        return `Showing ${this._formatNumber(this.drillPageStart)}-${this._formatNumber(this.drillPageEnd)} of ${this._formatNumber(this.drillTotalCount)} grouped rows`;
    }

    get marginStatus() {
        return this.state.bundle?.meta?.margin_status || {};
    }

    get marginCoveragePct() {
        const raw = Number(this.marginStatus.coverage_pct || 0);
        return Number.isFinite(raw) ? raw.toFixed(1) : "0.0";
    }

    get marginStatusClass() {
        return this.marginStatus.available ? "is-good" : "is-warn";
    }

    get marginStatusLabel() {
        if (this.marginStatus.available) {
            return "Real COGS margin active";
        }
        return "Margin hidden";
    }

    get marginStatusReasonText() {
        const reason = this.marginStatus.reason || "";
        if (reason === "missing_schema") {
            return "Required COGS fields are missing in this database schema.";
        }
        if (reason === "no_product_lines") {
            return "No product invoice lines found for the selected filters.";
        }
        if (reason === "incomplete_cost_coverage") {
            return "Some scoped lines do not have `total_cost`; margin is hidden to avoid proxy values.";
        }
        if (reason === "ok") {
            return "Margin is calculated from line-level `price_subtotal` and `total_cost`.";
        }
        return "Margin source check is unavailable.";
    }

    get dailySnapshot() {
        return this.state.bundle?.sections?.daily_snapshot || { rows: [], stats: {} };
    }

    get dailySnapshotRows() {
        return this.dailySnapshot.rows || [];
    }

    get dailySnapshotStats() {
        return this.dailySnapshot.stats || {};
    }

    get dailySnapshotBars() {
        const rows = this.dailySnapshotRows;
        if (!rows.length) {
            return [];
        }
        const maxValue = rows.reduce((max, row) => Math.max(max, Math.abs(Number(row.net_revenue || 0))), 0) || 1;
        return rows.map((row) => {
            const value = Number(row.net_revenue || 0);
            return {
                ...row,
                value,
                pct: Math.max(6, Math.round((Math.abs(value) / maxValue) * 100)),
            };
        });
    }

    get companyOptions() {
        return this.state.bundle?.filter_options?.companies || [];
    }

    get selectedCompanyLabels() {
        const optionMap = new Map(this.companyOptions.map((c) => [c.id, c.name]));
        return (this.state.filters.company_ids || []).map((id) => optionMap.get(id)).filter(Boolean);
    }

    get companySelectionSummary() {
        const selected = this.selectedCompanyLabels;
        if (!selected.length) {
            return "All accessible companies";
        }
        if (selected.length === 1) {
            return selected[0];
        }
        return `${selected.length} companies selected`;
    }

    get filteredCompanyOptions() {
        const q = String(this.state.companyPicker.search || "").trim().toLowerCase();
        if (!q) {
            return this.companyOptions;
        }
        return this.companyOptions.filter((c) => String(c.name || "").toLowerCase().includes(q));
    }

    get domainCatalog() {
        return this.state.bundle?.drill_catalog || [];
    }

    get selectedDomainCatalog() {
        return this.domainCatalog.find((d) => d.key === this.state.selectedDomain) || this.domainCatalog[0] || null;
    }

    get availableDomains() {
        return this.domainCatalog;
    }

    get availableGroups() {
        return this.selectedDomainCatalog?.groups || [];
    }

    get availableMetrics() {
        return this.selectedDomainCatalog?.metrics || [];
    }

    get selectedDomainCoverage() {
        return Number(this.coverage[this.state.selectedDomain] || 0);
    }

    get selectedDomainDescription() {
        return this.selectedDomainCatalog?.description || "";
    }

    get chartMetricColumn() {
        const columns = this.drillColumns || [];
        const preference = [
            this.state.selectedMetric,
            "net_revenue",
            "net_margin",
            "allocated_value",
            "weighted_pipeline",
            "open_pipeline",
            "average_basket",
            "on_hand_qty",
            "invoice_count",
            "open_opportunities",
        ];
        for (const key of preference) {
            if (columns.includes(key)) {
                return key;
            }
        }
        return columns.find((c) => c !== "dimension") || null;
    }

    get topChartRows() {
        const metric = this.chartMetricColumn;
        if (!metric) {
            return [];
        }
        const rows = [...(this.drillRows || [])]
            .filter((row) => row && row.dimension !== undefined && row[metric] !== undefined)
            .map((row) => ({
                dimension: row.dimension,
                metric,
                value: Number(row[metric] || 0),
            }))
            .sort((a, b) => b.value - a.value)
            .slice(0, 8);
        const maxValue = rows.reduce((m, r) => Math.max(m, Math.abs(r.value)), 0) || 1;
        return rows.map((row) => ({
            ...row,
            pct: Math.max(6, Math.round((Math.abs(row.value) / maxValue) * 100)),
        }));
    }

    get selectedMetricLabel() {
        const metric = this.availableMetrics.find((m) => m.key === this.state.selectedMetric);
        return metric?.label || this.state.selectedMetric;
    }

    get hasRows() {
        return (this.drillRows || []).length > 0;
    }

    get hasSort() {
        return Boolean(this.state.sort.column && this.state.sort.direction);
    }

    get sortSummary() {
        if (!this.hasSort) {
            return "Server default order";
        }
        const direction = this.state.sort.direction === "asc" ? "ascending" : "descending";
        return `Sorted by ${this.columnLabel(this.state.sort.column)} (${direction})`;
    }

    _syncSelectionFromBundle() {
        const domainCfg = this.selectedDomainCatalog;
        if (!domainCfg) {
            return;
        }

        const groupExists = (domainCfg.groups || []).some((g) => g.key === this.state.selectedGroupBy);
        if (!groupExists) {
            this.state.selectedGroupBy = domainCfg.default_group;
        }

        const metricExists = (domainCfg.metrics || []).some((m) => m.key === this.state.selectedMetric);
        if (!metricExists) {
            this.state.selectedMetric = domainCfg.default_metric;
        }
    }

    _syncCompanyDraft() {
        const validIds = new Set(this.companyOptions.map((c) => c.id));
        const current = Array.isArray(this.state.filters.company_ids) ? this.state.filters.company_ids : [];
        this.state.companyPicker.draft_ids = current.filter((id) => validIds.has(id));
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
            if (!this.state.filters.company_ids.length && bundle?.meta?.scope?.company_ids?.length) {
                this.state.filters.company_ids = [...bundle.meta.scope.company_ids];
            }
            this._syncCompanyDraft();
            this._syncSelectionFromBundle();
            await this._reloadDrilldown();
        } catch (error) {
            this.state.error = error?.message || "Failed to load dashboard";
        } finally {
            this.state.loading = false;
        }
    }

    async _reloadDrilldown() {
        try {
            const requestedLimit = this.drillLimit;
            const requestedOffset = this.drillOffset;
            const drilldown = await this.orm.call(
                "tradeline.executive.dashboard.service",
                "get_drilldown",
                [this.state.selectedDomain, this.state.selectedMetric, this.state.selectedGroupBy, this.state.filters, requestedLimit, requestedOffset]
            );
            const totalCount = Number(drilldown?.total_count || 0);
            if (totalCount > 0 && requestedOffset >= totalCount) {
                const lastOffset = Math.max(0, Math.floor((totalCount - 1) / requestedLimit) * requestedLimit);
                if (lastOffset !== requestedOffset) {
                    this.state.pagination.offset = lastOffset;
                    await this._reloadDrilldown();
                    return;
                }
            }
            if (this.state.bundle) {
                this.state.bundle.drilldown = drilldown;
            }
            this.state.pagination.limit = Number(drilldown?.limit || requestedLimit);
            this.state.pagination.offset = Number(drilldown?.offset || 0);
            if (this.state.sort.column && !(drilldown?.columns || []).includes(this.state.sort.column)) {
                this.state.sort.column = "";
                this.state.sort.direction = "";
            }
        } catch (_error) {
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
        if (value === null || value === undefined) {
            return "N/A";
        }
        const num = Number(value || 0);
        return `${num.toFixed(2)}%`;
    }

    _formatPercentOrDash(value) {
        if (value === null || value === undefined || Number.isNaN(Number(value))) {
            return "--";
        }
        return this._formatPercent(value);
    }

    _trendClass(value) {
        if (value === null || value === undefined || Number.isNaN(Number(value))) {
            return "neutral";
        }
        return Number(value) >= 0 ? "up" : "down";
    }

    _periodRows(periodChanges = {}) {
        return ["1D", "1M", "3M", "6M", "1Y"].map((label) => ({
            label,
            value: periodChanges?.[label],
        }));
    }

    _formatFxRate(value) {
        const num = Number(value || 0);
        return num.toFixed(6);
    }

    _shorten(text, maxLen = 26) {
        const input = String(text || "");
        return input.length > maxLen ? `${input.slice(0, maxLen - 1)}...` : input;
    }

    columnLabel(column) {
        const text = String(column || "").trim();
        if (!text) {
            return "";
        }
        return text
            .replace(/_/g, " ")
            .split(" ")
            .filter(Boolean)
            .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
            .join(" ");
    }

    _formatDayLabel(value) {
        const dt = new Date(`${value}T00:00:00`);
        if (Number.isNaN(dt.getTime())) {
            return value;
        }
        return dt.toLocaleDateString("en-EG", {
            weekday: "short",
            month: "short",
            day: "numeric",
        });
    }

    _formatCell(column, value) {
        if (value === null || value === undefined) {
            return "-";
        }
        const text = String(column || "");
        if (text.includes("revenue") || text.includes("value") || text.includes("pipeline") || text.includes("margin")) {
            if (text.includes("pct")) {
                return this._formatPercent(value);
            }
            return this._formatCurrency(value);
        }
        if (text.includes("rate") || text.includes("cost")) {
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
        this.state.pagination.offset = 0;
        await this._loadBundle();
    }

    async onDomainChange(ev) {
        this.state.selectedDomain = ev.target.value;
        this.state.pagination.offset = 0;
        this._syncSelectionFromBundle();
        await this._reloadDrilldown();
    }

    async onGroupChange(ev) {
        this.state.selectedGroupBy = ev.target.value;
        this.state.pagination.offset = 0;
        await this._reloadDrilldown();
    }

    async onMetricChange(ev) {
        this.state.selectedMetric = ev.target.value;
        this.state.pagination.offset = 0;
        await this._reloadDrilldown();
    }

    async onPageSizeChange(ev) {
        const nextLimit = Number(ev.target.value || 30);
        if (!Number.isFinite(nextLimit) || nextLimit <= 0 || nextLimit === this.drillLimit) {
            return;
        }
        this.state.pagination.limit = nextLimit;
        this.state.pagination.offset = 0;
        await this._reloadDrilldown();
    }

    async onPrevPage() {
        if (!this.hasPrevPage) {
            return;
        }
        this.state.pagination.offset = Math.max(0, this.drillOffset - this.drillLimit);
        await this._reloadDrilldown();
    }

    async onNextPage() {
        if (!this.hasNextPage) {
            return;
        }
        this.state.pagination.offset = this.drillOffset + this.drillLimit;
        await this._reloadDrilldown();
    }

    onSortColumn(column) {
        if (!column) {
            return;
        }
        if (this.state.sort.column !== column) {
            this.state.sort.column = column;
            this.state.sort.direction = "asc";
            return;
        }
        if (this.state.sort.direction === "asc") {
            this.state.sort.direction = "desc";
            return;
        }
        if (this.state.sort.direction === "desc") {
            this.state.sort.column = "";
            this.state.sort.direction = "";
            return;
        }
        this.state.sort.direction = "asc";
    }

    onSortColumnClick(ev) {
        const column = ev?.currentTarget?.dataset?.column || "";
        this.onSortColumn(column);
    }

    clearSort() {
        this.state.sort.column = "";
        this.state.sort.direction = "";
    }

    sortIcon(column) {
        if (this.state.sort.column !== column) {
            return "-";
        }
        return this.state.sort.direction === "asc" ? "^" : "v";
    }

    onToggleCompanyPicker() {
        this.state.companyPicker.open = !this.state.companyPicker.open;
        if (this.state.companyPicker.open) {
            this._syncCompanyDraft();
        }
    }

    onCompanySearchInput(ev) {
        this.state.companyPicker.search = ev.target.value || "";
    }

    onDraftCompanyToggle(ev) {
        const id = Number(ev.target.value);
        if (!Number.isFinite(id)) {
            return;
        }
        const selected = new Set(this.state.companyPicker.draft_ids || []);
        if (ev.target.checked) {
            selected.add(id);
        } else {
            selected.delete(id);
        }
        this.state.companyPicker.draft_ids = [...selected];
    }

    onSelectAllCompanies() {
        this.state.companyPicker.draft_ids = this.companyOptions.map((c) => c.id);
    }

    onClearCompanySelection() {
        this.state.companyPicker.draft_ids = [];
    }

    async onApplyCompanySelection() {
        this.state.filters.company_ids = [...(this.state.companyPicker.draft_ids || [])];
        this.state.companyPicker.open = false;
        this.state.pagination.offset = 0;
        await this._loadBundle();
    }

    async onDateChange() {
        this.state.pagination.offset = 0;
        await this._loadBundle();
    }

    async onRefreshFx() {
        this.state.refreshingFx = true;
        try {
            await this.orm.call("tradeline.executive.dashboard.service", "refresh_fx_rates", []);
            await this._loadBundle();
            this.notification.add("FX rates refreshed", { type: "success" });
        } catch (_error) {
            this.notification.add("FX refresh failed, showing last good rates", { type: "warning" });
            await this._loadBundle();
        } finally {
            this.state.refreshingFx = false;
        }
    }

    async openNativeView(domain) {
        const map = {
            finance: { name: "Invoices", model: "account.move", domain: [["move_type", "in", ["out_invoice", "out_receipt", "out_refund"]]] },
            sales: { name: "Invoices", model: "account.move", domain: [["move_type", "in", ["out_invoice", "out_receipt", "out_refund"]]] },
            inventory: { name: "Stock Quants", model: "stock.quant", domain: [] },
            pipeline: { name: "Opportunities", model: "crm.lead", domain: [["type", "=", "opportunity"]] },
        };
        const target = map[domain] || map.finance;
        await this.action.doAction({
            type: "ir.actions.act_window",
            name: target.name,
            res_model: target.model,
            views: [[false, "list"], [false, "form"]],
            view_mode: "list,form",
            domain: target.domain,
            context: {},
            target: "current",
        });
    }

    async onOpenNativeView() {
        await this.openNativeView(this.state.selectedDomain);
    }
}

ExecutivePocketDashboard.template = "tradeline_executive_pocket_dashboard.MainDashboard";
registry.category("actions").add("tradeline_executive_pocket_dashboard.main", ExecutivePocketDashboard);

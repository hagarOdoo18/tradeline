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
            topN: 10,
            drilldownOpen: false,
            companyPicker: { open: false, search: "", draft_ids: [] },
            filters: {
                start_date: this._formatDate(start),
                end_date: this._formatDate(today),
                company_ids: [],
                branch_ids: [],
                salesperson_ids: [],
            },
            selectedDomain: "finance",
            selectedGroupBy: "branch",
            selectedMetric: "net_revenue",
            sort: { column: "", direction: "" },
            pagination: { limit: 25, offset: 0 },
            lens: "overview",
            bundle: null,
            error: "",
        });

        onWillStart(async () => { await this._loadBundle(); });
    }

    // ─── Top-section data getters ─────────────────────────────────────────────
    get topSections() { return this.state.bundle?.top_sections || {}; }
    get topSalesByBranch() { return this.topSections.sales_by_branch || []; }
    get topSalesBySalesperson() { return this.topSections.sales_by_salesperson || []; }
    get topSalesByCategory() { return this.topSections.sales_by_category || []; }
    get topSalesByCustomer() { return this.topSections.sales_by_customer || []; }
    get topInventoryByCategory() { return this.topSections.inventory_by_category || []; }
    get salesOverMonth() { return this.topSections.sales_over_month || []; }
    get attachmentRate() { return Number(this.topSections.attachment_rate || 0); }
    get totalInvoices() { return Number(this.topSections.total_invoices || 0); }
    get accSales() { return Number(this.topSections.acc_sales || 0); }
    get accSalesPrevDay() { return Number(this.topSections.acc_sales_prev_day || 0); }
    get todaySales() { return Number(this.topSections.today_sales || 0); }
    get yesterdaySales() { return Number(this.topSections.yesterday_sales || 0); }
    get marginAvailableTop() { return Boolean(this.topSections.margin_available); }
    get companyNamesForReport() { return this.topSections.company_names || []; }

    // ─── KPI / meta getters ───────────────────────────────────────────────────
    get cards() { return this.state.bundle?.cards || []; }
    get alerts() { return this.state.bundle?.alerts || []; }
    get fxCards() { return this.state.bundle?.fx_watch?.cards || []; }
    get marginStatus() { return this.state.bundle?.meta?.margin_status || {}; }
    get marginCoveragePct() { return Number(this.marginStatus.coverage_pct || 0).toFixed(1); }
    get marginStatusClass() { return this.marginStatus.available ? "is-good" : "is-warn"; }
    get marginStatusLabel() { return this.marginStatus.available ? "✓ Real COGS margin" : "⚠ Margin approx."; }

    // ─── Daily snapshot ───────────────────────────────────────────────────────
    get dailySnapshot() { return this.state.bundle?.sections?.daily_snapshot || { rows: [], stats: {} }; }
    get dailySnapshotRows() { return this.dailySnapshot.rows || []; }
    get dailySnapshotStats() { return this.dailySnapshot.stats || {}; }
    get dailySnapshotBars() {
        const rows = this.dailySnapshotRows;
        if (!rows.length) return [];
        const max = rows.reduce((m, r) => Math.max(m, Math.abs(Number(r.net_revenue || 0))), 0) || 1;
        return rows.map(row => ({
            ...row,
            value: Number(row.net_revenue || 0),
            pct: Math.max(5, Math.round((Math.abs(Number(row.net_revenue || 0)) / max) * 100)),
        }));
    }

    // ─── Company picker ───────────────────────────────────────────────────────
    get companyOptions() { return this.state.bundle?.filter_options?.companies || []; }
    get selectedCompanyLabels() {
        const map = new Map(this.companyOptions.map(c => [c.id, c.name]));
        return (this.state.filters.company_ids || []).map(id => map.get(id)).filter(Boolean);
    }
    get companySelectionSummary() {
        const s = this.selectedCompanyLabels;
        if (!s.length) return "All accessible companies";
        if (s.length === 1) return s[0];
        return `${s.length} companies`;
    }
    get filteredCompanyOptions() {
        const q = String(this.state.companyPicker.search || "").trim().toLowerCase();
        return q ? this.companyOptions.filter(c => String(c.name || "").toLowerCase().includes(q)) : this.companyOptions;
    }

    // ─── Drilldown getters ────────────────────────────────────────────────────
    get drillRows() {
        const rows = this.state.bundle?.drilldown?.rows || [];
        const { column, direction } = this.state.sort;
        if (!column || !direction) return rows;
        const dir = direction === "asc" ? 1 : -1;
        return [...rows].sort((a, b) => {
            const lv = a?.[column], rv = b?.[column];
            if (lv == null) return 1; if (rv == null) return -1;
            return (typeof lv === "number" && typeof rv === "number") ? (lv - rv) * dir : String(lv).localeCompare(String(rv)) * dir;
        });
    }
    get drillColumns() { return this.state.bundle?.drilldown?.columns || []; }
    get drillTotalCount() { return Number(this.state.bundle?.drilldown?.total_count || 0); }
    get drillLimit() { return Number(this.state.pagination.limit || 25); }
    get drillOffset() { return Number(this.state.pagination.offset || 0); }
    get drillTotalPages() { return Math.max(1, Math.ceil(this.drillTotalCount / this.drillLimit)); }
    get drillCurrentPage() { return Math.floor(this.drillOffset / this.drillLimit) + 1; }
    get hasPrevPage() { return this.drillOffset > 0; }
    get hasNextPage() { return this.drillOffset + this.drillLimit < this.drillTotalCount; }
    get drillPageSummary() {
        if (!this.drillTotalCount) return "No rows found";
        const from = this.drillOffset + 1;
        const to = Math.min(this.drillOffset + this.drillLimit, this.drillTotalCount);
        return `Showing ${this._formatNumber(from)}–${this._formatNumber(to)} of ${this._formatNumber(this.drillTotalCount)} rows`;
    }
    get hasRows() { return (this.drillRows || []).length > 0; }
    get pageSizeOptions() { return [25, 50, 100]; }
    get domainCatalog() { return this.state.bundle?.drill_catalog || []; }
    get selectedDomainCatalog() { return this.domainCatalog.find(d => d.key === this.state.selectedDomain) || this.domainCatalog[0] || null; }
    get availableDomains() { return this.domainCatalog; }
    get availableGroups() { return this.selectedDomainCatalog?.groups || []; }
    get availableMetrics() { return this.selectedDomainCatalog?.metrics || []; }
    get hasSort() { return Boolean(this.state.sort.column && this.state.sort.direction); }
    get selectedDomainCoverage() { return Number((this.state.bundle?.coverage || {})[this.state.selectedDomain] || 0); }

    // ─── Computed chart data (cached as getters) ──────────────────────────────
    get donutCategorySegments() { return this._buildDonutSegments(this.topSalesByCategory, "net_revenue"); }
    get donutCategoryTotal() {
        return this.topSalesByCategory.reduce((s, d) => s + Math.max(0, Number(d.net_revenue || 0)), 0);
    }
    get lineChartData() { return this._buildLineChart(this.salesOverMonth, "net_revenue"); }
    get branchBarRows() { return this._barRowsFor(this.topSalesByBranch, "net_revenue"); }
    get salespersonBarRows() { return this._barRowsFor(this.topSalesBySalesperson, "net_revenue"); }
    get inventoryBarRows() { return this._barRowsFor(this.topInventoryByCategory, "allocated_value"); }
    get customerBarRows() { return this._barRowsFor(this.topSalesByCustomer, "net_revenue"); }
    get salesOverMonthFirstDate() { return this.salesOverMonth[0]?.date || ""; }
    get salesOverMonthMidDate() {
        const d = this.salesOverMonth; return d.length > 2 ? d[Math.floor(d.length / 2)]?.date || "" : "";
    }
    get salesOverMonthLastDate() {
        const d = this.salesOverMonth; return d.length > 1 ? d[d.length - 1]?.date || "" : "";
    }
    get todayVsYesterdayPct() { return this._percentChange(this.todaySales, this.yesterdaySales); }
    get accSalesVsPrevPct() { return this._percentChange(this.accSales, this.accSalesPrevDay); }
    get reportCompanyName() {
        const n = this.companyNamesForReport;
        return n.length ? n.join(" / ") : "Company";
    }

    // ─── Chart builders ───────────────────────────────────────────────────────
    _buildDonutSegments(data, valueKey) {
        const COLORS = ["#1d4ed8","#0ea5e9","#059669","#d97706","#dc2626","#7c3aed","#0891b2","#ea580c","#be185d","#166534","#0f766e","#b45309"];
        const total = data.reduce((s, d) => s + Math.max(0, Number(d[valueKey] || 0)), 0);
        if (!total) return [];
        let cum = 0;
        const R = 58, cx = 75, cy = 75;
        return data.map((item, i) => {
            const val = Math.max(0, Number(item[valueKey] || 0));
            const pct = val / total;
            const startDeg = cum * 360;
            const endDeg = (cum + pct) * 360;
            cum += pct;
            const toRad = d => (d - 90) * Math.PI / 180;
            const x1 = cx + R * Math.cos(toRad(startDeg));
            const y1 = cy + R * Math.sin(toRad(startDeg));
            const x2 = cx + R * Math.cos(toRad(endDeg));
            const y2 = cy + R * Math.sin(toRad(endDeg));
            const largeArc = endDeg - startDeg > 180 ? 1 : 0;
            const path = pct >= 0.9999
                ? `M ${cx} ${cy - R} A ${R} ${R} 0 1 1 ${(cx - 0.01).toFixed(2)} ${cy - R} Z`
                : `M ${cx} ${cy} L ${x1.toFixed(2)} ${y1.toFixed(2)} A ${R} ${R} 0 ${largeArc} 1 ${x2.toFixed(2)} ${y2.toFixed(2)} Z`;
            return { path, color: COLORS[i % COLORS.length], label: String(item.dimension || ""), value: val, pctLabel: (pct * 100).toFixed(1) };
        });
    }

    _buildLineChart(data, valueKey) {
        const W = 600, H = 140, pL = 8, pR = 8, pT = 16, pB = 10;
        const cW = W - pL - pR, cH = H - pT - pB;
        const values = data.map(d => Number(d[valueKey] || 0));
        const maxV = Math.max(...values, 1);
        const n = data.length;
        if (!n) return { lineD: "", areaD: "", points: [], maxV: 0, W, H };
        const points = data.map((d, i) => ({
            x: pL + (n <= 1 ? cW / 2 : (i / (n - 1)) * cW),
            y: pT + (1 - Number(d[valueKey] || 0) / maxV) * cH,
            ...d,
        }));
        const lineD = points.map((p, i) => `${i === 0 ? "M" : "L"} ${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(" ");
        const areaD = `${lineD} L ${points[n-1].x.toFixed(1)} ${(pT+cH).toFixed(1)} L ${pL} ${(pT+cH).toFixed(1)} Z`;
        return { lineD, areaD, points, maxV, W, H };
    }

    _barRowsFor(data, metricKey) {
        if (!data.length) return [];
        const max = Math.max(...data.map(d => Math.abs(Number(d[metricKey] || 0))), 1);
        return data.map((item, i) => ({
            ...item,
            _rank: i + 1,
            _val: Number(item[metricKey] || 0),
            pct: Math.max(4, Math.round((Math.abs(Number(item[metricKey] || 0)) / max) * 100)),
        }));
    }

    // ─── Formatters ───────────────────────────────────────────────────────────
    _formatDate(d) {
        return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
    }
    _formatCurrency(value) {
        return new Intl.NumberFormat("en-EG", { style: "currency", currency: "EGP", maximumFractionDigits: 0 }).format(Number(value || 0));
    }
    _formatNumber(value) {
        return new Intl.NumberFormat("en-EG", { maximumFractionDigits: 2 }).format(Number(value || 0));
    }
    _formatPercent(value) {
        return `${Number(value || 0).toFixed(1)}%`;
    }
    _formatPercentOrDash(value) {
        if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
        return `${Number(value).toFixed(1)}%`;
    }
    _formatCompact(value) {
        const num = Number(value || 0);
        const abs = Math.abs(num);
        const sign = num < 0 ? "-" : "";
        if (abs >= 1e9) return `${sign}${(abs/1e9).toFixed(1)}B`;
        if (abs >= 1e6) return `${sign}${(abs/1e6).toFixed(1)}M`;
        if (abs >= 1e3) return `${sign}${(abs/1e3).toFixed(0)}K`;
        return `${sign}${abs.toFixed(0)}`;
    }
    _formatCompactEGP(value) { return `EGP ${this._formatCompact(value)}`; }
    _formatFxRate(value) { return Number(value || 0).toFixed(4); }
    _trendClass(value) {
        if (value === null || value === undefined || Number.isNaN(Number(value))) return "neutral";
        return Number(value) >= 0 ? "up" : "down";
    }
    _trendIcon(value) {
        if (value === null || value === undefined || Number.isNaN(Number(value))) return "●";
        return Number(value) >= 0 ? "▲" : "▼";
    }
    _shorten(text, maxLen) {
        const s = String(text || "");
        return s.length > maxLen ? `${s.slice(0, maxLen - 1)}…` : s;
    }
    columnLabel(col) {
        return String(col || "").replace(/_/g, " ").split(" ").filter(Boolean).map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(" ");
    }
    _formatDayLabel(value) {
        const dt = new Date(`${value}T00:00:00`);
        if (Number.isNaN(dt.getTime())) return value;
        return dt.toLocaleDateString("en-EG", { weekday: "short", month: "short", day: "numeric" });
    }
    _formatShortDate(value) {
        const dt = new Date(`${value}T00:00:00`);
        if (Number.isNaN(dt.getTime())) return value;
        return dt.toLocaleDateString("en-EG", { month: "short", day: "numeric" });
    }
    _formatCell(column, value) {
        if (value === null || value === undefined) return "-";
        const t = String(column || "");
        if ((t.includes("revenue") || t.includes("value") || t.includes("margin") || t.includes("basket")) && !t.includes("pct")) return this._formatCurrency(value);
        if (t.includes("pct") || t.includes("percent") || t.includes("rate")) return this._formatPercent(value);
        if (typeof value === "number") return this._formatNumber(value);
        return value;
    }
    _periodRows(periodChanges) {
        return ["1D", "1M", "3M", "6M", "1Y"].map(label => ({ label, value: (periodChanges || {})[label] }));
    }
    _percentChange(current, previous) {
        if (!previous) return null;
        return ((current - previous) / previous) * 100;
    }
    sortIcon(col) {
        if (this.state.sort.column !== col) return "⇅";
        return this.state.sort.direction === "asc" ? "↑" : "↓";
    }

    // ─── Data loading ─────────────────────────────────────────────────────────
    async _loadBundle() {
        this.state.loading = true;
        this.state.error = "";
        try {
            const bundle = await this.orm.call(
                "tradeline.executive.dashboard.service",
                "get_dashboard_bundle",
                [this.state.filters, this.state.lens, null, this.state.topN]
            );
            this.state.bundle = bundle;
            if (!this.state.filters.company_ids.length && bundle?.meta?.scope?.company_ids?.length) {
                this.state.filters.company_ids = [...bundle.meta.scope.company_ids];
            }
            this._syncCompanyDraft();
            this._syncSelectionFromBundle();
            await this._reloadDrilldown();
        } catch (error) {
            this.state.error = error?.message || "Failed to load dashboard data.";
        } finally {
            this.state.loading = false;
        }
    }

    async _loadTopSections() {
        try {
            const topSections = await this.orm.call(
                "tradeline.executive.dashboard.service",
                "get_top_sections",
                [this.state.filters, this.state.topN]
            );
            if (this.state.bundle) this.state.bundle.top_sections = topSections;
        } catch {
            this.notification.add("Failed to refresh top sections", { type: "warning" });
        }
    }

    async _reloadDrilldown() {
        try {
            const drilldown = await this.orm.call(
                "tradeline.executive.dashboard.service",
                "get_drilldown",
                [this.state.selectedDomain, this.state.selectedMetric, this.state.selectedGroupBy, this.state.filters, this.drillLimit, this.drillOffset]
            );
            if (this.state.bundle) this.state.bundle.drilldown = drilldown;
            this.state.pagination.limit = Number(drilldown?.limit || this.drillLimit);
            this.state.pagination.offset = Number(drilldown?.offset || 0);
        } catch {
            this.notification.add("Failed to load drilldown data", { type: "warning" });
        }
    }

    _syncSelectionFromBundle() {
        const cfg = this.selectedDomainCatalog;
        if (!cfg) return;
        if (!(cfg.groups || []).some(g => g.key === this.state.selectedGroupBy)) this.state.selectedGroupBy = cfg.default_group;
        if (!(cfg.metrics || []).some(m => m.key === this.state.selectedMetric)) this.state.selectedMetric = cfg.default_metric;
    }
    _syncCompanyDraft() {
        const valid = new Set(this.companyOptions.map(c => c.id));
        this.state.companyPicker.draft_ids = (this.state.filters.company_ids || []).filter(id => valid.has(id));
    }

    // ─── Event handlers ───────────────────────────────────────────────────────
    async onTopNChange(ev) {
        this.state.topN = Number(ev.target.value || 10);
        await this._loadTopSections();
    }
    async onDateChange() { this.state.pagination.offset = 0; await this._loadBundle(); }
    async onRefreshFx() {
        this.state.refreshingFx = true;
        try {
            await this.orm.call("tradeline.executive.dashboard.service", "refresh_fx_rates", []);
            await this._loadBundle();
            this.notification.add("FX rates refreshed", { type: "success" });
        } catch { this.notification.add("FX refresh failed", { type: "warning" }); await this._loadBundle(); }
        finally { this.state.refreshingFx = false; }
    }
    onExportDailyReport() { window.print(); }
    onToggleCompanyPicker() {
        this.state.companyPicker.open = !this.state.companyPicker.open;
        if (this.state.companyPicker.open) this._syncCompanyDraft();
    }
    onCompanySearchInput(ev) { this.state.companyPicker.search = ev.target.value || ""; }
    onDraftCompanyToggle(ev) {
        const id = Number(ev.target.value);
        if (!Number.isFinite(id)) return;
        const s = new Set(this.state.companyPicker.draft_ids || []);
        ev.target.checked ? s.add(id) : s.delete(id);
        this.state.companyPicker.draft_ids = [...s];
    }
    onSelectAllCompanies() { this.state.companyPicker.draft_ids = this.companyOptions.map(c => c.id); }
    onClearCompanySelection() { this.state.companyPicker.draft_ids = []; }
    async onApplyCompanySelection() {
        this.state.filters.company_ids = [...(this.state.companyPicker.draft_ids || [])];
        this.state.companyPicker.open = false;
        this.state.pagination.offset = 0;
        await this._loadBundle();
    }
    onToggleDrilldown() { this.state.drilldownOpen = !this.state.drilldownOpen; }
    async onDomainChange(ev) { this.state.selectedDomain = ev.target.value; this.state.pagination.offset = 0; this._syncSelectionFromBundle(); await this._reloadDrilldown(); }
    async onGroupChange(ev) { this.state.selectedGroupBy = ev.target.value; this.state.pagination.offset = 0; await this._reloadDrilldown(); }
    async onMetricChange(ev) { this.state.selectedMetric = ev.target.value; this.state.pagination.offset = 0; await this._reloadDrilldown(); }
    async onPageSizeChange(ev) {
        const next = Number(ev.target.value || 25);
        if (!Number.isFinite(next) || next <= 0 || next === this.drillLimit) return;
        this.state.pagination.limit = next; this.state.pagination.offset = 0; await this._reloadDrilldown();
    }
    async onPrevPage() { if (!this.hasPrevPage) return; this.state.pagination.offset = Math.max(0, this.drillOffset - this.drillLimit); await this._reloadDrilldown(); }
    async onNextPage() { if (!this.hasNextPage) return; this.state.pagination.offset = this.drillOffset + this.drillLimit; await this._reloadDrilldown(); }
    onSortColumnClick(ev) {
        const col = ev?.currentTarget?.dataset?.column || "";
        if (!col) return;
        if (this.state.sort.column !== col) { this.state.sort.column = col; this.state.sort.direction = "asc"; return; }
        if (this.state.sort.direction === "asc") { this.state.sort.direction = "desc"; return; }
        this.state.sort.column = ""; this.state.sort.direction = "";
    }
    clearSort() { this.state.sort.column = ""; this.state.sort.direction = ""; }
    async onOpenNativeView() {
        const map = {
            finance: { name: "Invoices", model: "account.move", domain: [["move_type","in",["out_invoice","out_receipt","out_refund"]]] },
            sales: { name: "Invoices", model: "account.move", domain: [["move_type","in",["out_invoice","out_receipt","out_refund"]]] },
            inventory: { name: "Stock Quants", model: "stock.quant", domain: [] },
        };
        const t = map[this.state.selectedDomain] || map.finance;
        await this.action.doAction({ type: "ir.actions.act_window", name: t.name, res_model: t.model, views: [[false,"list"],[false,"form"]], view_mode: "list,form", domain: t.domain, context: {}, target: "current" });
    }
}

ExecutivePocketDashboard.template = "tradeline_executive_pocket_dashboard.MainDashboard";
registry.category("actions").add("tradeline_executive_pocket_dashboard.main", ExecutivePocketDashboard);

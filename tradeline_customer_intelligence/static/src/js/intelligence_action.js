/** @odoo-module **/

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, onWillStart, useState } from "@odoo/owl";

export class TradelineCustomerIntelligence extends Component {
    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.searchTimer = null;
        this.state = useState({
            activeView: "product",
            loading: true,
            exporting: false,
            error: "",
            query: "iPhone 17",
            searchInput: "iPhone 17",
            selectedEntity: { type: "query", id: 0, name: "iPhone 17", source: "auto" },
            suggestions: [],
            suggestionsOpen: false,
            source: "auto",
            startDate: "2025-12-01",
            endDate: "2025-12-31",
            bundle: null,
            comparison: null,
            comparisonLoading: false,
            comparisonError: "",
            selectedCompanionKey: null,
            selectedCustomerKey: null,
            audienceOpen: false,
            navCollapsed: false,
        });
        onWillStart(async () => this.loadProduct());
    }

    get navItems() {
        return [
            { key: "product", label: "Product 360" },
            { key: "comparison", label: "Legacy vs Live" },
            { key: "customer", label: "Customer 360" },
            { key: "bundle", label: "Bundle Lab" },
            { key: "audience", label: "Audience Builder" },
            { key: "launch", label: "Launch Cockpit" },
            { key: "quality", label: "Data Quality" },
        ];
    }
    get bundle() { return this.state.bundle || {}; }
    get product() { return this.bundle.product || { name: this.state.query, grain_label: "Search match" }; }
    get summary() { return this.bundle.summary || {}; }
    get companions() { return this.bundle.companions || []; }
    get customers() { return this.bundle.customers || []; }
    get paymentMix() { return this.bundle.payment_mix || []; }
    get dimensions() { return this.bundle.dimensions || {}; }
    get trend() { return this.dimensions.trend || []; }
    get storeMix() { return this.dimensions.store_mix || []; }
    get salespersonMix() { return this.dimensions.salesperson_mix || []; }
    get discountMix() { return this.dimensions.discount_mix || []; }
    get channelMix() { return this.dimensions.channel_mix || []; }
    get customerSegments() { return this.bundle.customer_segments || []; }
    get coverageSources() { return this.bundle.coverage?.sources || []; }
    get recommendation() { return this.bundle.recommendation || {}; }
    get comparison() { return this.state.comparison || {}; }
    get comparisonMonths() { return this.comparison.months || []; }
    get selectedCompanion() {
        return this.companions.find(row => row.product_key === this.state.selectedCompanionKey) || this.companions[0] || null;
    }
    get selectedCustomer() {
        return this.customers.find(row => row.customer_key === this.state.selectedCustomerKey) || this.customers[0] || null;
    }
    get maxAttachRate() {
        return Math.max(...this.companions.map(row => Number(row.attach_rate || 0)), 1);
    }
    get sourceButtonLabel() {
        if (this.state.source === "current") return "Odoo 18 live";
        if (this.state.source === "legacy") return "Odoo 12 archive";
        return "Best available source";
    }
    get launchSentence() {
        if (!this.selectedCompanion) return "Expand the period or select another product to reveal a launch opportunity.";
        return `Lead with ${this.selectedCompanion.product_name}; retarget identified ${this.product.name} owners for the next upgrade cycle.`;
    }
    get paymentTotal() {
        return this.paymentMix.reduce((total, row) => total + Number(row.baskets || 0), 0);
    }
    get emailReady() { return this.customers.filter(customer => customer.email).length; }
    get mobileReady() { return this.customers.filter(customer => customer.mobile).length; }
    get priorityCustomers() { return this.customers.filter(customer => customer.segment === "Priority").length; }
    get topStore() { return this.storeMix[0] || null; }

    navClass(key) {
        return `tl-intel-nav-item ${this.state.activeView === key ? "is-active" : ""}`;
    }
    rowClass(row) {
        return `tl-affinity-row ${this.selectedCompanion?.product_key === row.product_key ? "is-selected" : ""}`;
    }
    sourceClass(source) {
        return `tl-source-dot is-${source.status || "empty"}`;
    }
    attachWidth(row) {
        return `${Math.max(4, Number(row.attach_rate || 0) / this.maxAttachRate * 100)}%`;
    }
    paymentWidth(row) {
        return `${this.paymentTotal ? Number(row.baskets || 0) / this.paymentTotal * 100 : 0}%`;
    }
    paymentClass(row) {
        const key = String(row.name || "other").toLowerCase().replace(/[^a-z]/g, "");
        return `tl-payment-segment is-${key || "other"}`;
    }
    segmentClass(segment) {
        return `tl-segment is-${String(segment || "core").toLowerCase()}`;
    }
    dimensionWidth(row, rows) {
        const maximum = Math.max(...rows.map(item => Number(item.baskets || 0)), 1);
        return `${Math.max(3, Number(row.baskets || 0) / maximum * 100)}%`;
    }
    formatNumber(value) {
        return new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(Number(value || 0));
    }
    formatCurrency(value) {
        return new Intl.NumberFormat("en-US", { style: "currency", currency: "EGP", maximumFractionDigits: 0 }).format(Number(value || 0));
    }
    formatPercent(value) {
        return `${Number(value || 0).toFixed(2)}%`;
    }
    formatLift(value) {
        return `${Number(value || 0).toFixed(2)}×`;
    }
    formatSignedPercent(value) {
        if (value === null || value === undefined) return "—";
        const amount = Number(value || 0);
        return `${amount > 0 ? "+" : ""}${amount.toFixed(1)}%`;
    }
    round(value) { return Math.round(Number(value || 0)); }
    extractError(error) {
        return error?.data?.message || error?.message || "The intelligence engine could not load this scope.";
    }

    async loadProduct() {
        this.state.loading = true;
        this.state.error = "";
        this.state.suggestionsOpen = false;
        try {
            const bundle = await this.orm.call(
                "tradeline.customer.intelligence.service",
                "get_product_360",
                [this.state.query, this.state.startDate, this.state.endDate, this.state.source, 20, this.state.selectedEntity]
            );
            this.state.bundle = bundle;
            this.state.selectedCompanionKey = bundle.companions?.[0]?.product_key || null;
            this.state.selectedCustomerKey = bundle.customers?.[0]?.customer_key || null;
        } catch (error) {
            this.state.error = this.extractError(error);
        } finally {
            this.state.loading = false;
        }
    }

    async loadComparison() {
        this.state.comparisonLoading = true;
        this.state.comparisonError = "";
        try {
            this.state.comparison = await this.orm.call(
                "tradeline.customer.intelligence.service",
                "get_legacy_comparison",
                [this.state.query, this.state.selectedEntity]
            );
        } catch (error) {
            this.state.comparisonError = this.extractError(error);
        } finally {
            this.state.comparisonLoading = false;
        }
    }

    async onNavigate(ev) {
        this.state.activeView = ev.currentTarget.dataset.view;
        if (this.state.activeView === "comparison" && !this.state.comparison) {
            await this.loadComparison();
        }
    }
    onToggleNav() {
        this.state.navCollapsed = !this.state.navCollapsed;
    }
    onSearchInput(ev) {
        this.state.searchInput = ev.target.value;
        clearTimeout(this.searchTimer);
        const query = this.state.searchInput.trim();
        if (query.length < 2) {
            this.state.suggestions = [];
            this.state.suggestionsOpen = false;
            return;
        }
        this.searchTimer = setTimeout(async () => {
            try {
                this.state.suggestions = await this.orm.call(
                    "tradeline.customer.intelligence.service",
                    "search_entities",
                    [query, 10]
                );
                this.state.suggestionsOpen = true;
            } catch {
                this.state.suggestions = [];
            }
        }, 180);
    }
    async onSearchKeydown(ev) {
        if (ev.key === "Enter") {
            ev.preventDefault();
            const query = this.state.searchInput.trim();
            if (query.length >= 2) {
                this.state.query = query;
                this.state.selectedEntity = { type: "query", id: 0, name: query, source: "auto" };
                this.state.comparison = null;
                await this.loadProduct();
                if (this.state.activeView === "comparison") await this.loadComparison();
            }
        } else if (ev.key === "Escape") {
            this.state.suggestionsOpen = false;
        }
    }
    async onSelectSuggestion(ev) {
        const key = ev.currentTarget.dataset.key;
        const selected = this.state.suggestions.find(item => item.key === key);
        if (!selected) return;
        this.state.searchInput = selected.name;
        this.state.query = selected.name;
        this.state.selectedEntity = {
            type: selected.type,
            id: Number(selected.id || 0),
            name: selected.name,
            source: selected.source,
        };
        this.state.comparison = null;
        await this.loadProduct();
        if (this.state.activeView === "comparison") await this.loadComparison();
    }
    async onDateChange() {
        await this.loadProduct();
    }
    async onSourceChange(ev) {
        this.state.source = ev.target.value;
        await this.loadProduct();
    }
    onSelectCompanion(ev) {
        this.state.selectedCompanionKey = ev.currentTarget.dataset.key;
    }
    onSelectCustomer(ev) {
        this.state.selectedCustomerKey = ev.currentTarget.dataset.key;
    }
    onBuildAudience() {
        this.state.audienceOpen = true;
        this.state.activeView = "audience";
    }
    onCloseAudience() {
        this.state.audienceOpen = false;
    }
    onOpenLaunch() {
        this.state.activeView = "launch";
    }
    async onExport() {
        this.state.exporting = true;
        try {
            const action = await this.orm.call(
                "tradeline.customer.intelligence.service",
                "export_product_insight",
                [this.state.query, this.state.startDate, this.state.endDate, this.state.source, this.state.selectedEntity]
            );
            await this.action.doAction(action);
        } catch (error) {
            this.notification.add(this.extractError(error), { type: "danger" });
        } finally {
            this.state.exporting = false;
        }
    }
}

TradelineCustomerIntelligence.template = "tradeline_customer_intelligence.Main";
registry.category("actions").add("tradeline_customer_intelligence.main", TradelineCustomerIntelligence);

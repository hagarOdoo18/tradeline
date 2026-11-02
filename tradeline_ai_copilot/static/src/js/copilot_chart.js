/** @odoo-module **/
import { Component, onWillStart, onWillUnmount, useEffect, useRef } from "@odoo/owl";
import { loadJS } from "@web/core/assets";

export class CopilotChart extends Component {
    static template = "tradeline_ai_copilot.Chart";
    static props = { block: Object };
    setup() {
        this.canvas = useRef("canvas");
        onWillStart(async () => {
            if (!window.Chart) { await loadJS("/web/static/lib/Chart/Chart.js"); }
        });
        useEffect(() => { this.draw(); return () => this.destroy(); }, () => [this.props.block]);
        onWillUnmount(() => this.destroy());
    }
    destroy() { this.chart?.destroy(); this.chart = null; }
    get horizontal() {
        const block = this.props.block;
        return block.chart_type === "bar" && (block.data.labels.length > 7 || block.data.labels.some(label => label.length > 28));
    }
    get canvasStyle() {
        return this.horizontal ? `height: ${Math.max(240, this.props.block.data.labels.length * 28 + 48)}px` : "";
    }
    downloadChart() {
        if (!this.canvas.el || !this.chart) { return; }
        const block = this.props.block;
        const source = this.canvas.el;
        const canvas = document.createElement("canvas");
        canvas.width = Math.max(1000, source.width);
        const ratio = canvas.width / source.width;
        canvas.height = source.height * ratio + 120;
        const ctx = canvas.getContext("2d");
        ctx.fillStyle = "#ffffff";
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.fillStyle = "#0f172a";
        ctx.font = "600 20px sans-serif";
        ctx.fillText(block.title || "Tradeline AI report", 24, 32, canvas.width - 48);
        ctx.drawImage(source, 0, 55, canvas.width, source.height * ratio);
        ctx.fillStyle = "#64748b";
        ctx.font = "14px sans-serif";
        ctx.fillText(["Tradeline AI", block.source_id, block.queried_at ? `Queried ${block.queried_at} UTC` : "",
            block.truncated ? "Limited chart preview; consult supporting table and sources" : "See supporting table for exact values and scope"].filter(Boolean).join(" · "),
        24, canvas.height - 24, canvas.width - 48);
        const link = document.createElement("a");
        link.download = `${(block.title || "tradeline-ai-chart").replace(/[^a-z0-9_-]+/gi, "-").slice(0, 100)}.png`;
        link.href = canvas.toDataURL("image/png");
        link.click();
    }
    draw() {
        this.destroy();
        const block = this.props.block;
        const colors = ["#2563eb", "#0891b2", "#7c3aed", "#059669", "#d97706", "#e11d48"];
        const kind = ["bar", "line", "doughnut"].includes(block.chart_type) ? block.chart_type : "bar";
        const doughnut = kind === "doughnut";
        const integers = block.integer_values ?? block.data.datasets.every(set => set.data.every(Number.isInteger));
        this.chart = new window.Chart(this.canvas.el, {
            type: kind,
            data: { labels: block.data.labels, datasets: block.data.datasets.map((set, index) => ({
                ...set, backgroundColor: doughnut ? colors : colors[index % colors.length],
                borderColor: colors[index % colors.length], borderWidth: kind === "line" ? 2 : 0,
                borderRadius: kind === "bar" ? 4 : 0, maxBarThickness: 72, tension: 0.15, pointRadius: 3,
            })) },
            options: { responsive: true, maintainAspectRatio: false, animation: false,
                indexAxis: this.horizontal ? "y" : "x",
                plugins: { legend: { display: doughnut || block.data.datasets.length > 1, position: "bottom" }, tooltip: { enabled: true } },
                scales: doughnut ? {} : {
                    x: { beginAtZero: this.horizontal, grid: { display: this.horizontal, color: "#eef2f6" }, ticks: { maxRotation: 0, autoSkip: !this.horizontal, color: "#64748b", precision: this.horizontal && integers ? 0 : undefined } },
                    y: { beginAtZero: !this.horizontal, grid: { display: !this.horizontal, color: "#eef2f6" }, ticks: { autoSkip: false, color: "#64748b", precision: !this.horizontal && integers ? 0 : undefined,
                        callback: this.horizontal ? function(value) {
                            const label = this.getLabelForValue(value);
                            if (this.chart.width < 500 && label.length > 22) {
                                return [label.slice(0, 22), label.slice(22, 42) + (label.length > 42 ? "…" : "")];
                            }
                            return label.length > 65 ? label.slice(0, 62) + "…" : label;
                        } : undefined } },
                },
            },
        });
    }
}

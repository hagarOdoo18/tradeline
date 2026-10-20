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
                plugins: { legend: { display: doughnut || block.data.datasets.length > 1, position: "bottom" }, tooltip: { enabled: true } },
                scales: doughnut ? {} : {
                    x: { grid: { display: false }, ticks: { maxRotation: 35, autoSkip: true, color: "#64748b" } },
                    y: { beginAtZero: true, grid: { color: "#eef2f6" }, ticks: { color: "#64748b", precision: integers ? 0 : undefined } },
                },
            },
        });
    }
}

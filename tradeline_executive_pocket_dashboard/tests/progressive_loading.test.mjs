import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

// Exercise the shipped controller with mocked Odoo services, without a browser
// or production database. Run: node --test <this file>
const source = readFileSync(new URL("../static/src/js/executive_dashboard_action.js", import.meta.url), "utf8")
    .replace(/^import .*;\r?\n/gm, "")
    .replace("export class ExecutivePocketDashboard", "class ExecutivePocketDashboard")
    + "\nglobalThis.Dashboard = ExecutivePocketDashboard;";
const shell = () => ({ meta: { scope: { company_ids: [1] } }, top_sections: {}, sections: {}, cards: [], drill_catalog: [] });
const deferred = () => {
    let resolve, reject;
    const promise = new Promise((a, b) => { resolve = a; reject = b; });
    return { promise, resolve, reject };
};
const flush = async () => { for (let i = 0; i < 12; i++) await new Promise(setImmediate); };

function controller(call) {
    const hooks = {};
    const classes = new Set();
    const context = vm.createContext({
        Date, Map, Set, WeakSet, JSON, Object, Number, String, Math, Promise,
        Component: class {}, useState: value => value, useRef: () => ({ el: null }),
        useService: name => name === "orm" ? { call } : { add() {}, doAction() {} },
        onMounted: fn => { hooks.mount = fn; }, onPatched: fn => { hooks.patch = fn; },
        onWillUnmount: fn => { hooks.unmount = fn; },
        registry: { category: () => ({ add() {} }) },
        document: { body: { classList: { add: x => classes.add(x), remove: (...xs) => xs.forEach(x => classes.delete(x)) } } },
        window: { print: () => { context.printed = true; } }, requestAnimationFrame: fn => fn(),
    });
    vm.runInContext(source, context);
    const dashboard = new context.Dashboard();
    dashboard.setup();
    return { dashboard, hooks, context, classes };
}

test("mount immediately, fetch shell, deduplicate sections, and leave drilldown closed", async () => {
    const calls = [], pending = deferred();
    const { dashboard, hooks } = controller(async (model, method, args) => {
        calls.push([method, args?.[0]]);
        if (method === "get_dashboard_shell") return pending.promise;
        return { top_sections: {} };
    });
    assert.equal(hooks.mount(), undefined);
    assert.equal(dashboard.state.loading, true);
    assert.equal(calls.length, 1);
    pending.resolve(shell());
    await flush();
    dashboard._enqueueSection("sales_by_branch");
    dashboard._enqueueSection("sales_by_branch");
    await flush();
    assert.equal(dashboard.state.loading, false);
    assert.equal(calls.filter(([method]) => method === "get_drilldown").length, 0);
    assert.equal(calls.filter(([, section]) => section === "sales_by_branch").length, 1);
    assert.equal(calls.filter(([, section]) => section === "sales_by_product").length, 0);
});

test("slow results from previous filters never overwrite the current page", async () => {
    const old = deferred();
    const { dashboard } = controller(async (model, method, args) => {
        if (method === "get_dashboard_shell") return shell();
        if (args[0] === "overview" && args[1].start_date === "2026-01-01") return old.promise;
        return { top_sections: { today_sales: 200 } };
    });
    dashboard.state.filters.start_date = "2026-01-01";
    await dashboard._loadBundle();
    dashboard.state.filters.start_date = "2026-02-01";
    await dashboard._loadBundle();
    await flush();
    old.resolve({ top_sections: { today_sales: 999 } });
    await flush();
    assert.equal(dashboard.topSections.today_sales, 200);
});

test("failed sections show an error and retry without discarding successful sections", async () => {
    let fail = true;
    const { dashboard } = controller(async (model, method, args) => {
        if (method === "get_dashboard_shell") return shell();
        if (args[0] === "sales_by_product" && fail) throw new Error("temporarily unavailable");
        return { top_sections: { [args[0]]: [{ dimension: "Ready" }] } };
    });
    await dashboard._loadBundle(); await flush();
    dashboard._enqueueSection("sales_by_branch");
    dashboard._enqueueSection("sales_by_product");
    await flush();
    assert.equal(dashboard.state.sectionStatus.sales_by_product, "error");
    assert.equal(dashboard.topSalesByBranch.length, 1);
    fail = false;
    dashboard.onRetrySection("sales_by_product"); await flush();
    assert.equal(dashboard.state.sectionStatus.sales_by_product, "ready");
    assert.equal(dashboard.topSalesByBranch.length, 1);
});

test("unmount ignores late RPC completion", async () => {
    const pending = deferred();
    const { dashboard, hooks } = controller(() => pending.promise);
    hooks.mount(); hooks.unmount();
    pending.resolve(shell()); await flush();
    assert.equal(dashboard.state.bundle, null);
});

test("print waits for the complete bundle and restores print classes", async () => {
    const pending = deferred();
    const { dashboard, context, classes } = controller(() => pending.promise);
    dashboard.state.bundle = shell();
    const printing = dashboard._printReport("daily");
    assert.equal(context.printed, undefined);
    pending.resolve({ ...shell(), daily_top_sections: { today_sales: 123 } });
    await printing;
    assert.equal(context.printed, true);
    assert.equal(dashboard.dailyTodaySales, 123);
    assert.equal(dashboard.state.exporting, false);
    assert.equal(classes.size, 0);
});

test("older drilldown requests cannot overwrite the newest result", async () => {
    const first = deferred(), second = deferred(); let count = 0;
    const { dashboard } = controller(() => ++count === 1 ? first.promise : second.promise);
    dashboard.state.bundle = shell();
    const oldRequest = dashboard._reloadDrilldown();
    const newRequest = dashboard._reloadDrilldown();
    second.resolve({ rows: [{ dimension: "New" }], limit: 25, offset: 0 });
    await newRequest;
    first.resolve({ rows: [{ dimension: "Old" }], limit: 25, offset: 0 });
    await oldRequest;
    assert.equal(dashboard.drillRows[0].dimension, "New");
    assert.equal(dashboard.state.drillLoading, false);
});

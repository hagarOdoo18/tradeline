import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";
import vm from "node:vm";

// Run with ODOO_SOURCE_DIR pointing to an Odoo 18 checkout:
// node --test pos_discount_reason/tests/payment_screen.test.mjs
// Exercise Odoo's real validation/finalization and patch utility. Only services,
// order data, and the server synchronization boundary are replaced by fixtures.
const odooSource = process.env.ODOO_SOURCE_DIR;
assert.ok(odooSource, "Set ODOO_SOURCE_DIR to an Odoo 18 source checkout");
const coreSource = readFileSync(join(odooSource,
    "addons/point_of_sale/static/src/app/screens/payment_screen/payment_screen.js"), "utf8")
    .replace(/^import .*;\r?$/gm, "")
    .replace("export class PaymentScreen", "globalThis.PaymentScreen = class PaymentScreen");
const patchSource = readFileSync(join(odooSource,
    "addons/web/static/src/core/utils/patch.js"), "utf8")
    .replace(/export function /g, "function ");
const extensionSource = readFileSync(new URL("../static/src/js/payment_screen.js", import.meta.url), "utf8")
    .replace(/^import .*;\r?$/gm, "");

function fixture({ autoInvoice = true, invoice = false, partner = false, confirm = false, total = 1100 } = {}) {
    const dialogs = [];
    const submissions = [];
    const events = [];
    const context = vm.createContext({
        Component: class {}, Numpad: class {}, PaymentScreenPaymentLines: class {},
        PaymentScreenStatus: class {}, AlertDialog: class {}, ConfirmationDialog: class {},
        ConnectionLostError: class extends Error {}, RPCError: class extends Error {},
        _t: value => value, floatIsZero: value => Math.abs(value) < 0.00001,
        serializeDateTime: () => "2026-11-05 14:00:00",
        luxon: { DateTime: { now() {} } },
        registry: { category: () => ({ add() {} }) }, console,
    });
    // Use Odoo's actual confirmation helper as well as its screen implementation.
    const dialogSource = readFileSync(join(odooSource,
        "addons/point_of_sale/static/src/app/store/make_awaitable_dialog.js"), "utf8")
        .replace(/^import .*;\r?$/gm, "").replace(/export function /g, "function ");
    vm.runInContext(dialogSource, context);
    vm.runInContext(coreSource, context);
    vm.runInContext(patchSource, context);
    vm.runInContext(extensionSource, context);

    const line = { discount: 0, qty: 1, product_id: { uom_id: { rounding: 1 } } };
    const order = {
        id: 1, state: "draft", sales_rep_id: 1, invoice, partner, lines: [line],
        payment_ids: [{ amount: total, is_done: () => true, payment_method_id: { split_transactions: false } }],
        get_orderlines() { return this.lines; }, get_partner() { return this.partner; },
        is_to_invoice() { return this.invoice; }, set_to_invoice(value) { this.invoice = value; },
        get_total_with_tax: () => total, get_total_paid: () => total,
        get_rounding_applied: () => 0, getShippingDate: () => false,
        is_paid: () => true, _isValidEmptyOrder: () => true,
        is_paid_with_cash: () => false, get_change: () => 0,
    };
    const screen = new context.PaymentScreen();
    screen.props = { orderUuid: "regression-test" };
    screen.pos = {
        config: { auto_invoice: autoInvoice, auto_print_invoice: false },
        currency: { decimal_places: 2 }, models: { "pos.order": { getBy: () => order } },
        selectPartner: () => events.push("selectPartner"),
        editPartner: () => events.push("editPartner"), addPendingOrder: () => events.push("pending"),
        syncAllOrders: async () => {
            submissions.push({ invoice: order.invoice, partner: order.partner });
            return [];
        },
    };
    screen.dialog = { add(Component, props) {
        dialogs.push(props);
        if (props.confirm) {
            (confirm ? props.confirm : props.cancel)();
        }
    } };
    screen.numberBuffer = { capture() {} };
    screen.env = { services: { ui: { block: () => events.push("block"), unblock() {} } } };
    screen.check_cash_rounding_has_been_well_applied = () => true;
    screen.shouldDownloadInvoice = () => false;
    screen.afterOrderValidation = async () => events.push("validated");
    return { screen, order, dialogs, submissions, events };
}

function validPartner(values = {}) {
    return { id: 42, name: "Regression customer", vat: "123456789", company_type: "person",
        street: "Test street", city: "Cairo", country_id: 1, state_id: 1, ...values };
}

test("automatic invoice without customer stops before synchronization", async () => {
    const f = fixture();
    await f.screen.validateOrder(false);
    assert.equal(f.submissions.length, 0);
    assert.equal(f.order.state, "draft");
    assert.equal(f.order.invoice, true);
    assert.equal(f.dialogs[0].title, "Please select the Customer");
    assert.equal(f.events.includes("pending"), false);
});

test("forced validation also requires the automatic invoice customer", async () => {
    const f = fixture();
    await f.screen.validateOrder(true);
    assert.equal(f.submissions.length, 0);
    assert.equal(f.dialogs[0].title, "Please select the Customer");
});

test("customer selection then retry submits the same order exactly once", async () => {
    const f = fixture({ confirm: true });
    await f.screen.validateOrder(false);
    assert.equal(f.submissions.length, 0);
    assert.deepEqual(f.events, ["selectPartner"]);
    const customer = validPartner();
    f.order.partner = customer;
    await f.screen.validateOrder(false);
    assert.equal(f.submissions.length, 1);
    assert.equal(f.submissions[0].invoice, true);
    assert.equal(f.submissions[0].partner, customer);
    assert.equal(f.events.filter(event => event === "validated").length, 1);
});

test("automatic invoice preserves the selected customer", async () => {
    const customer = validPartner();
    const f = fixture({ partner: customer });
    await f.screen.validateOrder(false);
    assert.equal(f.submissions.length, 1);
    assert.equal(f.submissions[0].invoice, true);
    assert.equal(f.submissions[0].partner, customer);
    assert.equal(f.dialogs.length, 0);
});

test("manual invoice still requires a customer with automatic invoicing disabled", async () => {
    const f = fixture({ autoInvoice: false, invoice: true });
    await f.screen.validateOrder(false);
    assert.equal(f.submissions.length, 0);
    assert.equal(f.dialogs[0].title, "Please select the Customer");
});

test("non-invoiced anonymous sale remains allowed when automatic invoicing is disabled", async () => {
    const f = fixture({ autoInvoice: false });
    await f.screen.validateOrder(false);
    assert.equal(f.submissions.length, 1);
    assert.equal(f.submissions[0].invoice, false);
    assert.equal(f.submissions[0].partner, false);
});

for (const [name, prepare, title] of [
    ["discount reason", f => { f.order.lines[0].discount = 10; }, "Missing Discount Reason"],
    ["sales representative", f => { f.order.sales_rep_id = false; }, "Missing Field"],
    ["company VAT", f => { f.order.partner = validPartner({ vat: false, company_type: "company" }); }, "Missing Field"],
    ["customer address", f => { f.order.partner = validPartner({ street: false }); }, "Incorrect address for shipping"],
]) {
    test(`automatic invoicing preserves ${name} validation`, async () => {
        const f = fixture({ partner: validPartner() });
        prepare(f);
        await f.screen.validateOrder(false);
        assert.equal(f.submissions.length, 0);
        assert.equal(f.dialogs[0].title, title);
    });
}

test("high-value individual invoice still requires identification", async () => {
    const f = fixture({ partner: validPartner({ vat: false }), total: 15000 });
    await f.screen.validateOrder(false);
    assert.equal(f.submissions.length, 0);
    assert.equal(f.dialogs[0].body, "An Identification Number Is Required");
});

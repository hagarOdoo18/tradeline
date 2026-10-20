// Run: node --experimental-vm-modules --test pos_serial_validation/tests/serial_search.test.mjs
// Exercises the shipped screen patch without a browser or database.
import { readFile } from 'node:fs/promises';
import { SourceTextModule } from 'node:vm';
import assert from 'node:assert/strict';
import { test } from 'node:test';

const source = await readFile(new URL('../static/src/js/product_screen.js', import.meta.url), 'utf8');
const modules = {
    '@point_of_sale/app/screens/product_screen/product_screen': `export class ProductScreen {
        setup() {} async _barcodeProductAction() { this.nativeScans++; }
        async addProductToOrder() { this.nativeClicks++; }
        async onPressEnterKey() { this.nativeEnters++; }
    }`,
    '@web/core/utils/patch': `export function patch(target, extension) {
        const previous = Object.create(Object.getPrototypeOf(target));
        Object.defineProperties(previous, Object.getOwnPropertyDescriptors(target));
        Object.setPrototypeOf(extension, previous);
        Object.defineProperties(target, Object.getOwnPropertyDescriptors(extension));
    }`,
    '@odoo/owl': 'export const onWillUnmount = () => {}; export const useEffect = () => {}; export const useState = x => x;',
    '@point_of_sale/app/navbar/navbar': 'export class Navbar { setup() {} }',
    '@web/core/l10n/translation': `export const _t = (text, ...args) => args.reduce((s, a) => s.replace('%s', a), text);`,
    '@web/core/confirmation_dialog/confirmation_dialog': 'export class AlertDialog {}',
    '@web/core/network/rpc': 'export class ConnectionLostError extends Error {}',
};
const linked = new Map(Object.entries(modules).map(([name, text]) => [name, new SourceTextModule(text)]));
const module = new SourceTextModule(source);
await module.link(name => { assert.ok(linked.has(name), name); return linked.get(name); });
await module.evaluate();
const { ProductScreen } = linked.get('@point_of_sale/app/screens/product_screen/product_screen').namespace;
const { ConnectionLostError } = linked.get('@web/core/network/rpc').namespace;
const navbarSource = await readFile(new URL('../static/src/js/navbar.js', import.meta.url), 'utf8');
// Navbar lifecycle registration is irrelevant to the event-handler contract.
const navbarOwl = new SourceTextModule('export const onMounted = () => {}; export const onPatched = () => {}; export const onWillUnmount = () => {};');
const navbarModule = new SourceTextModule(navbarSource);
await navbarModule.link(name => name === '@odoo/owl' ? navbarOwl : linked.get(name));
await navbarModule.evaluate();
const { Navbar } = linked.get('@point_of_sale/app/navbar/navbar').namespace;

function fixture({ offline = false, tracking = 'serial', orders = [], lots } = {}) {
    const screen = new ProductScreen();
    screen.nativeScans = screen.nativeClicks = screen.nativeEnters = 0;
    const product = { id: 42, tracking, needToConfigure: () => false };
    const lines = [];
    const current = { finalized: false, get_orderlines: () => lines };
    const events = { added: 0, rpc: 0, alerts: [], resets: 0 };
    screen.dialog = { add: (_dialog, props) => events.alerts.push(props) };
    screen.numberBuffer = { reset: () => events.resets++ };
    screen.pos = {
        config: { id: 67, enable_product_bar_lot_serial_search: true },
        models: {
            'pos.order': [current, ...orders],
            'stock.lot': lots || [{ name: 'SER-1', product_id: product, product_qty: 1 }],
            'product.product': { get: id => id === product.id ? product : undefined },
        },
        data: { network: { offline }, call: async () => {
            events.rpc++;
            return { products: [{ product_id: 42, matched_lots: ['SER-1'] }] };
        } },
        addLineToCurrentOrder: async (_values, options) => {
            events.added++;
            lines.push({ product_id: product, qty: 1, pack_lot_ids: [{ lot_name: options.code.code }] });
        },
        searchProductWord: 'SER-1',
    };
    screen.setup();
    return { screen, product, lines, events, exact: { product, lotName: 'SER-1' } };
}

test('exact serial adds once; repeat is handled without native fallback', async () => {
    const f = fixture();
    assert.equal(await f.screen._tryAddLotSerialSearchMatch('SER-1'), true);
    assert.equal(await f.screen._tryAddLotSerialSearchMatch('SER-1'), true);
    assert.equal(f.events.added, 1);
    assert.equal(f.events.alerts.length, 1);
    assert.match(f.events.alerts[0].body, /SER-1/);
    assert.equal(f.screen.pos.searchProductWord, '');
});

test('simultaneous Enter/click/scan cannot insert a serial twice', async () => {
    const f = fixture();
    let release;
    const nativeAdd = f.screen.pos.addLineToCurrentOrder;
    f.screen.pos.addLineToCurrentOrder = async (...args) => {
        await new Promise(resolve => { release = resolve; });
        return nativeAdd(...args);
    };
    const first = f.screen._addLotSerialMatchToCurrentOrder(f.exact);
    await f.screen._addLotSerialMatchToCurrentOrder(f.exact);
    assert.equal(f.events.alerts.length, 1);
    release(); await first;
    assert.equal(f.events.added, 1);
    assert.equal(f.screen.pos._pendingLotSerialAdds.size, 0);
});

test('failed or cancelled configuration releases pending serial', async () => {
    const f = fixture();
    const nativeAdd = f.screen.pos.addLineToCurrentOrder;
    f.screen.pos.addLineToCurrentOrder = async () => { throw new Error('configuration failed'); };
    await assert.rejects(f.screen._addLotSerialMatchToCurrentOrder(f.exact));
    assert.equal(f.screen.pos._pendingLotSerialAdds.size, 0);
    f.screen.pos.addLineToCurrentOrder = nativeAdd;
    await f.screen._addLotSerialMatchToCurrentOrder(f.exact);
    assert.equal(f.events.added, 1);
});

test('different serials on a merged product line remain allowed', async () => {
    const f = fixture();
    await f.screen._addLotSerialMatchToCurrentOrder(f.exact);
    await f.screen._addLotSerialMatchToCurrentOrder({ product: f.product, lotName: 'SER-2' });
    assert.equal(f.events.added, 2);
    assert.equal(f.events.alerts.length, 0);
});

test('serial already on another open order is blocked', async () => {
    const f = fixture();
    f.screen.pos.models['pos.order'].push({ finalized: false, get_orderlines: () => [
        { product_id: f.product, qty: 2, pack_lot_ids: [{ lot_name: 'SER-1' }, { lot_name: 'SER-2' }] },
    ] });
    await f.screen._addLotSerialMatchToCurrentOrder(f.exact);
    assert.equal(f.events.added, 0);
    assert.equal(f.events.alerts.length, 1);
});

for (const [name, options] of Object.entries({
    'finalized order': { finalized: true, qty: 1 },
    'negative return line': { qty: -1 },
    'linked refund line': { qty: 1, refunded_orderline_id: { id: 9 } },
    'different product': { qty: 1, differentProduct: true },
})) {
    test(`${name} does not block a valid new sale`, async () => {
        const f = fixture();
        f.screen.pos.models['pos.order'].push({ finalized: options.finalized || false,
            get_orderlines: () => [{ product_id: options.differentProduct ? { id: 99 } : f.product,
                qty: options.qty, refunded_orderline_id: options.refunded_orderline_id,
                pack_lot_ids: [{ lot_name: 'SER-1' }] }] });
        await f.screen._addLotSerialMatchToCurrentOrder(f.exact);
        assert.equal(f.events.added, 1);
    });
}

test('lot-tracked product retains repeated lot quantities', async () => {
    const f = fixture({ tracking: 'lot' });
    await f.screen._addLotSerialMatchToCurrentOrder(f.exact);
    await f.screen._addLotSerialMatchToCurrentOrder(f.exact);
    assert.equal(f.events.added, 2);
});

test('zero-quantity serial line still reserves its pack lot before native merging', async () => {
    const f = fixture();
    f.lines.push({ product_id: f.product, qty: 0, pack_lot_ids: [{ lot_name: 'SER-1' }] });
    await f.screen._addLotSerialMatchToCurrentOrder(f.exact);
    assert.equal(f.events.added, 0);
    assert.equal(f.events.alerts.length, 1);
});

test('offline exact lookup uses loaded stock and makes no RPC', async () => {
    const f = fixture({ offline: true });
    await f.screen._tryAddLotSerialSearchMatch(' ser-1 ');
    await f.screen._tryAddLotSerialSearchMatch('SER-1');
    assert.equal(f.events.added, 1);
    assert.equal(f.events.rpc, 0);
    assert.equal(f.events.alerts.length, 1);
});

test('connection loss falls back to cached serial; server errors do not', async () => {
    const f = fixture();
    f.screen.pos.data.call = async () => { throw new ConnectionLostError(); };
    assert.equal((await f.screen._fetchLotSerialMatches('SER-1')).matches.length, 1);
    f.screen.pos.data.call = async () => { throw new Error('Access denied'); };
    await assert.rejects(f.screen._fetchLotSerialMatches('SER-1'), /Access denied/);
});

test('offline unknown, partial, empty or unavailable serial does not auto-add', async () => {
    const f = fixture({ offline: true });
    for (const query of ['UNKNOWN', 'SER', '']) {
        assert.equal(await f.screen._resolveSingleExactLotSerialMatch(query), null);
    }
    for (const quantity of [0, -1, 2]) {
        f.screen.pos.models['stock.lot'][0].product_qty = quantity;
        assert.equal(await f.screen._resolveSingleExactLotSerialMatch('SER-1'), null);
    }
    assert.equal(f.events.added, 0);
});

test('offline ambiguous serial shared by products does not auto-resolve', async () => {
    const f = fixture({ offline: true });
    f.screen.pos.models['stock.lot'].push({ name: 'SER-1', product_id: { id: 99, tracking: 'serial' }, product_qty: 1 });
    f.screen.pos.models['product.product'].get = id => f.screen.pos.models['stock.lot'].find(lot => lot.product_id.id === id).product_id;
    assert.equal(await f.screen._resolveSingleExactLotSerialMatch('SER-1'), null);
});

test('scanner, click and Enter all use duplicate guard', async () => {
    const f = fixture();
    await f.screen._barcodeProductAction({ base_code: 'SER-1', code: 'SER-1' });
    f.screen.pos.searchProductWord = 'SER-1';
    await f.screen.addProductToOrder(f.product);
    f.screen.pos.searchProductWord = 'SER-1';
    await f.screen.onPressEnterKey();
    assert.equal(f.events.added, 1);
    assert.equal(f.events.alerts.length, 2);
    assert.equal(f.screen.nativeClicks + f.screen.nativeScans + f.screen.nativeEnters, 0);
});

test('normal barcode still uses native scanner path', async () => {
    const f = fixture();
    f.screen.pos.data.call = async () => ({ products: [] });
    await f.screen._barcodeProductAction({ base_code: 'PLAIN-BARCODE' });
    assert.equal(f.screen.nativeScans, 1);
    assert.equal(f.events.added, 0);
});

test('offline unsafe cached serial never falls through to another scanner patch', async () => {
    const f = fixture({ offline: true });
    f.screen.pos.models['stock.lot'][0].product_qty = 2;
    await f.screen._barcodeProductAction({ base_code: 'SER-1' });
    assert.equal(f.screen.nativeScans, 0);
    assert.equal(f.events.added, 0);
    assert.equal(f.events.alerts.length, 1);
});

test('rapid scanner Enter uses visible input before the model debounce', async () => {
    const navbar = new Navbar();
    const queries = [];
    const events = [];
    const input = { value: ' SER-NEW ', dispatchEvent: event => events.push(event.type) };
    navbar.pos = { config: { enable_product_bar_lot_serial_search: true },
        mainScreen: { component: { name: 'ProductScreen' } }, searchProductWord: 'SER-OLD',
        _addExactLotSerialSearchMatch: async query => { queries.push(query); return true; } };
    await navbar._onLotSerialSearchKeydown({ key: 'Enter', currentTarget: input,
        preventDefault() {}, stopPropagation() {} });
    assert.deepEqual(queries, ['SER-NEW']);
    assert.deepEqual(events, ['input']);
    assert.equal(input.value, '');
    assert.equal(navbar.pos.searchProductWord, '');
});

test('an empty input never submits an older debounced serial', async () => {
    const navbar = new Navbar();
    navbar.pos = { config: { enable_product_bar_lot_serial_search: true },
        mainScreen: { component: { name: 'ProductScreen' } }, searchProductWord: 'SER-OLD',
        _addExactLotSerialSearchMatch: async () => { assert.fail('stale serial submitted'); } };
    await navbar._onLotSerialSearchKeydown({ key: 'Enter', currentTarget: { value: '' } });
});

test('unknown serial keeps input intact for correction', async () => {
    const navbar = new Navbar();
    const input = { value: 'UNKNOWN', dispatchEvent: () => assert.fail('input cleared') };
    navbar.pos = { config: { enable_product_bar_lot_serial_search: true },
        mainScreen: { component: { name: 'ProductScreen' } }, searchProductWord: 'UNKNOWN',
        _addExactLotSerialSearchMatch: async () => false };
    await navbar._onLotSerialSearchKeydown({ key: 'Enter', currentTarget: input,
        preventDefault() {}, stopPropagation() {} });
    assert.equal(input.value, 'UNKNOWN');
});

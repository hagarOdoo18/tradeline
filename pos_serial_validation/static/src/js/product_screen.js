/** @odoo-module **/

import { ProductScreen } from "@point_of_sale/app/screens/product_screen/product_screen";
import { patch } from "@web/core/utils/patch";
import { onWillUnmount, useEffect, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { AlertDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { ConnectionLostError } from "@web/core/network/rpc";

const LIVE_SEARCH_DELAY_MS = 250;
const MIN_QUERY_LENGTH = 3;
const RESULT_LIMIT = 10;

patch(ProductScreen.prototype, {
    setup() {
        super.setup(...arguments);
        this.lotSerialSearch = useState({
            loading: false,
            query: "",
            productIds: [],
            matchedLotsByProductId: {},
        });
        this._lotSerialSearchRequestToken = 0;
        this._lotSerialSearchTimeout = null;
        this._posResolveExactLotSerialSearchMatch = (query, options = {}) =>
            this._resolveSingleExactLotSerialMatch(query, options);
        this._posAddExactLotSerialSearchMatch = (query, options = {}) =>
            this._tryAddLotSerialSearchMatch(query, options);
        this.pos._resolveExactLotSerialSearchMatch = this._posResolveExactLotSerialSearchMatch;
        this.pos._addExactLotSerialSearchMatch = this._posAddExactLotSerialSearchMatch;

        useEffect(
            () => {
                this._scheduleLotSerialSearch(this.searchWord);
            },
            () => [
                this.pos.config.enable_product_bar_lot_serial_search,
                this.pos.searchProductWord,
            ]
        );

        onWillUnmount(() => {
            this._clearLotSerialSearchTimeout();
            if (
                this.pos._resolveExactLotSerialSearchMatch ===
                this._posResolveExactLotSerialSearchMatch
            ) {
                delete this.pos._resolveExactLotSerialSearchMatch;
            }
            if (
                this.pos._addExactLotSerialSearchMatch ===
                this._posAddExactLotSerialSearchMatch
            ) {
                delete this.pos._addExactLotSerialSearchMatch;
            }
        });
    },

    _isLotSerialSearchEnabled() {
        return Boolean(this.pos.config.enable_product_bar_lot_serial_search);
    },

    _normalizeLotSerialQuery(searchWord) {
        return (searchWord || "").trim();
    },

    _clearLotSerialSearchTimeout() {
        if (this._lotSerialSearchTimeout) {
            clearTimeout(this._lotSerialSearchTimeout);
            this._lotSerialSearchTimeout = null;
        }
    },

    _resetLotSerialSearch() {
        this._clearLotSerialSearchTimeout();
        this.lotSerialSearch.loading = false;
        this.lotSerialSearch.query = "";
        this.lotSerialSearch.productIds = [];
        this.lotSerialSearch.matchedLotsByProductId = {};
    },

    _finalizeLotSerialAdd() {
        this.pos.searchProductWord = "";
        this._lotSerialSearchRequestToken += 1;
        this._resetLotSerialSearch();
    },

    _scheduleLotSerialSearch(searchWord) {
        const query = this._normalizeLotSerialQuery(searchWord);
        this._clearLotSerialSearchTimeout();

        if (!this._isLotSerialSearchEnabled() || query.length < MIN_QUERY_LENGTH) {
            this._lotSerialSearchRequestToken += 1;
            this._resetLotSerialSearch();
            return;
        }

        const requestToken = ++this._lotSerialSearchRequestToken;
        this.lotSerialSearch.loading = true;
        this._lotSerialSearchTimeout = setTimeout(() => {
            this._lotSerialSearchTimeout = null;
            this._performLotSerialSearch(query, requestToken);
        }, LIVE_SEARCH_DELAY_MS);
    },

    async _fetchLotSerialMatches(query, options = {}) {
        const normalizedQuery = this._normalizeLotSerialQuery(query);
        const { skipMinLength = false } = options;
        if (!skipMinLength && normalizedQuery.length < MIN_QUERY_LENGTH) {
            return {
                matches: [],
                matchedLotsByProductId: {},
                productIds: [],
                products: [],
            };
        }

        // An already loaded register can resolve exact serials without RPC.
        // Do not turn server access/validation errors into an offline fallback.
        if (this.pos.data.network.offline) {
            return this._getCachedLotSerialMatches(normalizedQuery);
        }
        let response;
        try {
            response = await this.pos.data.call(
                "stock.lot",
                "search_pos_products",
                [normalizedQuery, this.pos.config.id],
                { limit: RESULT_LIMIT }
            );
        } catch (error) {
            if (!(error instanceof ConnectionLostError)) {
                throw error;
            }
            return this._getCachedLotSerialMatches(normalizedQuery);
        }
        const matches = response?.products || [];
        const productIds = matches.map((item) => item.product_id);
        const missingProductIds = productIds.filter(
            (productId) => !this.pos.models["product.product"].get(productId)
        );

        if (missingProductIds.length) {
            const loadedProducts = await this.pos.data.searchRead(
                "product.product",
                [["id", "in", missingProductIds]],
                this.pos.data.fields["product.product"] || [],
                {
                    context: { display_default_code: false },
                }
            );
            await this.pos.processProductAttributesByProducts(loadedProducts);
        }

        const matchedLotsByProductId = {};
        for (const match of matches) {
            matchedLotsByProductId[match.product_id] = match.matched_lots || [];
        }

        return {
            matches,
            matchedLotsByProductId,
            productIds,
            products: productIds
                .map((productId) => this.pos.models["product.product"].get(productId))
                .filter(Boolean),
        };
    },

    _getCachedLotSerialMatches(query) {
        const normalizedQuery = query.toLowerCase();
        const matches = [];
        const matchedLotsByProductId = {};
        const products = [];
        for (const lot of this.pos.models["stock.lot"]?.filter(
            (lot) => (lot.name || "").toLowerCase() === normalizedQuery
        ) || []) {
            const product = lot.product_id;
            // Use only loaded, unambiguous serial stock. Current stock/location
            // and sold-state validation still happens on the server at sync.
            if (product?.tracking !== "serial" || lot.product_qty !== 1) {
                continue;
            }
            matches.push({ product_id: product.id, matched_lots: [lot.name] });
            matchedLotsByProductId[product.id] = [lot.name];
            products.push(product);
        }
        return {
            matches,
            matchedLotsByProductId,
            productIds: products.map((product) => product.id),
            products,
        };
    },

    async _performLotSerialSearch(query, requestToken) {
        try {
            const results = await this._fetchLotSerialMatches(query);
            if (requestToken !== this._lotSerialSearchRequestToken) {
                return;
            }

            this.lotSerialSearch.query = query.toLowerCase();
            this.lotSerialSearch.productIds = results.products.map((product) => product.id);
            this.lotSerialSearch.matchedLotsByProductId = results.matchedLotsByProductId;
        } catch (error) {
            if (requestToken !== this._lotSerialSearchRequestToken) {
                return;
            }
            console.error("Failed to search POS products by lot/serial number.", error);
            this.lotSerialSearch.query = "";
            this.lotSerialSearch.productIds = [];
            this.lotSerialSearch.matchedLotsByProductId = {};
        } finally {
            if (requestToken === this._lotSerialSearchRequestToken) {
                this.lotSerialSearch.loading = false;
            }
        }
    },

    _getLotSerialMatchedProducts(searchWord) {
        const normalizedQuery = this._normalizeLotSerialQuery(searchWord).toLowerCase();
        if (
            !this._isLotSerialSearchEnabled() ||
            normalizedQuery.length < MIN_QUERY_LENGTH ||
            this.lotSerialSearch.query !== normalizedQuery
        ) {
            return [];
        }

        return this.lotSerialSearch.productIds
            .map((productId) => this.pos.models["product.product"].get(productId))
            .filter(Boolean);
    },

    _getSingleExactLotSerialMatchFromResults(results, searchWord) {
        const normalizedQuery = this._normalizeLotSerialQuery(searchWord).toLowerCase();
        if (!normalizedQuery || !results?.matches?.length) {
            return null;
        }

        const exactMatches = [];
        for (const match of results.matches) {
            const product = this.pos.models["product.product"].get(match.product_id);
            if (!product) {
                continue;
            }
            const exactLotName = (match.matched_lots || []).find(
                (lotName) => (lotName || "").toLowerCase() === normalizedQuery
            );
            if (exactLotName) {
                exactMatches.push({
                    lotName: exactLotName,
                    product,
                });
            }
        }

        return exactMatches.length === 1 ? exactMatches[0] : null;
    },

    _getSingleExactLotSerialMatchFromState(searchWord) {
        const normalizedQuery = this._normalizeLotSerialQuery(searchWord).toLowerCase();
        if (
            !normalizedQuery ||
            normalizedQuery.length < MIN_QUERY_LENGTH ||
            this.lotSerialSearch.query !== normalizedQuery
        ) {
            return null;
        }

        const exactMatches = [];
        for (const productId of this.lotSerialSearch.productIds) {
            const product = this.pos.models["product.product"].get(productId);
            if (!product) {
                continue;
            }
            const exactLotName = (this.lotSerialSearch.matchedLotsByProductId[productId] || []).find(
                (lotName) => (lotName || "").toLowerCase() === normalizedQuery
            );
            if (exactLotName) {
                exactMatches.push({
                    lotName: exactLotName,
                    product,
                });
            }
        }

        return exactMatches.length === 1 ? exactMatches[0] : null;
    },

    _buildLotSerialCode(lotName) {
        return {
            base_code: lotName,
            code: lotName,
            type: "lot",
        };
    },

    async _addLotSerialMatchToCurrentOrder(exactMatch) {
        const { product, lotName } = exactMatch;
        const pending = this.pos._pendingLotSerialAdds ||= new Set();
        const key = JSON.stringify([product.id, lotName]);
        // Native merging retains pack lots on zero-quantity lines too.
        const alreadyUsed = product.tracking === "serial" && (
            pending.has(key) || this.pos.models["pos.order"].some((order) =>
                !order.finalized && order.get_orderlines().some((line) =>
                    line.product_id.id === product.id && line.qty >= 0 &&
                    !line.refunded_orderline_id &&
                    line.pack_lot_ids.some((lot) => lot.lot_name === lotName)
                )
            )
        );
        if (alreadyUsed) {
            this.dialog.add(AlertDialog, {
                title: _t("Serial already in an open order"),
                body: _t('Serial "%s" is already in an open order. Each serial can only be added once.', lotName),
            });
            this.numberBuffer.reset();
            return;
        }
        // Reserve before awaiting native configuration so overlapping Enter,
        // click and scanner events cannot both pass the duplicate check.
        pending.add(key);
        try {
            await this.pos.addLineToCurrentOrder(
                { product_id: product },
                { code: this._buildLotSerialCode(lotName) },
                product.needToConfigure()
            );
            this.numberBuffer.reset();
        } finally {
            pending.delete(key);
        }
    },

    async _resolveSingleExactLotSerialMatch(searchWord, options = {}) {
        const normalizedQuery = this._normalizeLotSerialQuery(searchWord);
        const { skipMinLength = false } = options;
        if (
            !this._isLotSerialSearchEnabled() ||
            !normalizedQuery ||
            (!skipMinLength && normalizedQuery.length < MIN_QUERY_LENGTH)
        ) {
            return null;
        }

        try {
            const results = await this._fetchLotSerialMatches(normalizedQuery, { skipMinLength });
            return this._getSingleExactLotSerialMatchFromResults(results, normalizedQuery);
        } catch (error) {
            console.error("Failed to resolve POS product by lot/serial number.", error);
            return null;
        }
    },

    async _tryAddLotSerialSearchMatch(searchWord, options = {}) {
        const normalizedQuery = this._normalizeLotSerialQuery(searchWord);
        const exactMatch =
            options.exactMatch || (await this._resolveSingleExactLotSerialMatch(normalizedQuery, options));
        if (!exactMatch) {
            return false;
        }

        try {
            await this._addLotSerialMatchToCurrentOrder(exactMatch);
            this._finalizeLotSerialAdd();
            return true;
        } catch (error) {
            console.error("Failed to add POS product by lot/serial number.", error);
            return false;
        }
    },

    getProductsBySearchWord(searchWord) {
        const nativeProducts = super.getProductsBySearchWord(...arguments);
        const exactMatch = this._getSingleExactLotSerialMatchFromState(searchWord);
        if (exactMatch) {
            return [exactMatch.product];
        }

        const lotSerialProducts = this._getLotSerialMatchedProducts(searchWord);
        if (!lotSerialProducts.length) {
            return nativeProducts;
        }

        const mergedProducts = [...lotSerialProducts];
        const seenProductIds = new Set(lotSerialProducts.map((product) => product.id));
        for (const product of nativeProducts) {
            if (!seenProductIds.has(product.id)) {
                seenProductIds.add(product.id);
                mergedProducts.push(product);
            }
        }
        return mergedProducts;
    },

    async loadProductFromDB() {
        const nativeProducts = (await super.loadProductFromDB(...arguments)) || [];
        const query = this._normalizeLotSerialQuery(this.pos.searchProductWord);
        if (!this._isLotSerialSearchEnabled() || query.length < MIN_QUERY_LENGTH) {
            return nativeProducts;
        }

        try {
            const lotSerialProducts = (await this._fetchLotSerialMatches(query)).products;
            const mergedProducts = [...nativeProducts];
            const seenProductIds = new Set(nativeProducts.map((product) => product.id));
            for (const product of lotSerialProducts) {
                if (!seenProductIds.has(product.id)) {
                    seenProductIds.add(product.id);
                    mergedProducts.push(product);
                }
            }
            return mergedProducts;
        } catch (error) {
            console.error("Failed to load POS products by lot/serial number.", error);
            return nativeProducts;
        }
    },

    async addProductToOrder(product) {
        const searchWord = this._normalizeLotSerialQuery(this.pos.searchProductWord);
        if (searchWord) {
            const exactMatch =
                this._getSingleExactLotSerialMatchFromState(searchWord) ||
                (await this._resolveSingleExactLotSerialMatch(searchWord, {
                    skipMinLength: true,
                }));
            if (exactMatch) {
                const added = await this._tryAddLotSerialSearchMatch(searchWord, {
                    exactMatch,
                    skipMinLength: true,
                });
                if (added) {
                    return;
                }
            }
        }

        return await super.addProductToOrder(...arguments);
    },

    async onPressEnterKey() {
        const searchWord = this._normalizeLotSerialQuery(this.pos.searchProductWord);
        if (!searchWord) {
            return;
        }

        if (await this._tryAddLotSerialSearchMatch(searchWord, { skipMinLength: true })) {
            return;
        }

        return await super.onPressEnterKey(...arguments);
    },

    async _barcodeProductAction(code) {
        const barcode = this._normalizeLotSerialQuery(code?.base_code || code?.code);
        if (barcode) {
            const exactMatch = await this._resolveSingleExactLotSerialMatch(barcode, {
                skipMinLength: true,
            });
            if (exactMatch) {
                await this._addLotSerialMatchToCurrentOrder(exactMatch);
                return;
            }
            if (this.pos.data.network.offline && this.pos.models["stock.lot"]?.some(
                (lot) => (lot.name || "").toLowerCase() === barcode.toLowerCase() &&
                    lot.product_id?.tracking === "serial"
            )) {
                // A known but ambiguous/unavailable cached serial must not
                // fall through to another scanner patch which selects the first lot.
                this.dialog.add(AlertDialog, {
                    title: _t("Serial unavailable offline"),
                    body: _t("This serial cannot be resolved safely from the loaded stock. Reconnect before adding it."),
                });
                return;
            }
        }

        return await super._barcodeProductAction(...arguments);
    },
});

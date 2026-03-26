/** @odoo-module **/

import { ProductScreen } from "@point_of_sale/app/screens/product_screen/product_screen";
import { patch } from "@web/core/utils/patch";
import { onWillUnmount, useEffect, useState } from "@odoo/owl";

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

    async _fetchLotSerialMatches(query) {
        const normalizedQuery = this._normalizeLotSerialQuery(query);
        if (normalizedQuery.length < MIN_QUERY_LENGTH) {
            return {
                matchedLotsByProductId: {},
                productIds: [],
                products: [],
            };
        }

        const response = await this.pos.data.call(
            "stock.lot",
            "search_pos_products",
            [normalizedQuery, this.pos.config.id],
            { limit: RESULT_LIMIT }
        );
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
            matchedLotsByProductId,
            productIds,
            products: productIds
                .map((productId) => this.pos.models["product.product"].get(productId))
                .filter(Boolean),
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

    _hasSingleExactLotSerialMatch(searchWord) {
        const normalizedQuery = this._normalizeLotSerialQuery(searchWord).toLowerCase();
        if (
            normalizedQuery.length < MIN_QUERY_LENGTH ||
            this.lotSerialSearch.query !== normalizedQuery ||
            this.lotSerialSearch.productIds.length !== 1
        ) {
            return false;
        }

        const productId = this.lotSerialSearch.productIds[0];
        const matchedLots = this.lotSerialSearch.matchedLotsByProductId[productId] || [];
        return matchedLots.some((lotName) => lotName.toLowerCase() === normalizedQuery);
    },

    _getSingleExactLotSerialMatch(results, searchWord) {
        const normalizedQuery = this._normalizeLotSerialQuery(searchWord).toLowerCase();
        if (
            normalizedQuery.length < MIN_QUERY_LENGTH ||
            !results?.products?.length ||
            results.products.length !== 1
        ) {
            return null;
        }

        const product = results.products[0];
        const matchedLots = results.matchedLotsByProductId?.[product.id] || [];
        const exactLotName = matchedLots.find(
            (lotName) => (lotName || "").toLowerCase() === normalizedQuery
        );
        if (!exactLotName) {
            return null;
        }

        return {
            lotName: exactLotName,
            product,
        };
    },

    _buildLotSerialCode(lotName) {
        return {
            base_code: lotName,
            code: lotName,
            type: "lot",
        };
    },

    async _tryAddLotSerialSearchMatch(searchWord) {
        const normalizedQuery = this._normalizeLotSerialQuery(searchWord);
        if (!this._isLotSerialSearchEnabled() || normalizedQuery.length < MIN_QUERY_LENGTH) {
            return false;
        }

        try {
            const results = await this._fetchLotSerialMatches(normalizedQuery);
            const exactMatch = this._getSingleExactLotSerialMatch(results, normalizedQuery);
            if (!exactMatch) {
                return false;
            }

            await this.pos.addLineToCurrentOrder(
                { product_id: exactMatch.product },
                { code: this._buildLotSerialCode(exactMatch.lotName) },
                exactMatch.product.needToConfigure()
            );
            this.numberBuffer.reset();
            this.pos.searchProductWord = "";
            this._lotSerialSearchRequestToken += 1;
            this._resetLotSerialSearch();
            return true;
        } catch (error) {
            console.error("Failed to add POS product by lot/serial number.", error);
            return false;
        }
    },

    getProductsBySearchWord(searchWord) {
        const nativeProducts = super.getProductsBySearchWord(...arguments);
        const lotSerialProducts = this._getLotSerialMatchedProducts(searchWord);
        if (!lotSerialProducts.length) {
            return nativeProducts;
        }
        if (this._hasSingleExactLotSerialMatch(searchWord)) {
            return lotSerialProducts;
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

    async onPressEnterKey() {
        const searchWord = this._normalizeLotSerialQuery(this.pos.searchProductWord);
        if (!searchWord) {
            return;
        }

        if (await this._tryAddLotSerialSearchMatch(searchWord)) {
            return;
        }

        return await super.onPressEnterKey(...arguments);
    },
});

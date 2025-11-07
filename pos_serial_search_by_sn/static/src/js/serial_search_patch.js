import { ProductScreen } from "@point_of_sale/app/screens/product_screen/product_screen";
import { patch } from "@web/core/utils/patch";

const _superOnBarcodeScanned = ProductScreen.prototype.onBarcodeScanned;
const _superUpdateSearch = ProductScreen.prototype._updateSearch;

patch(ProductScreen.prototype, {
    async onBarcodeScanned(code) {
        const sn = (code || "").trim();
        if (sn && this.env.pos.serialIndex && this.env.pos.serialIndex[sn]) {
            const productId = this.env.pos.serialIndex[sn];
            const product = this.env.pos.db.get_product_by_id(productId);
            if (product) {
                this.env.pos.get_order().add_product(product, { description:' SN ${sn}' });
                return;
            }
        }
        if (_superOnBarcodeScanned) {
            return await _superOnBarcodeScanned.call(this, code);
        }
    },

    _updateSearch(event) {
        const query = (event?.detail?.query || "").trim();
        if (query && this.env.pos.serialIndex && this.env.pos.serialIndex[query]) {
            const productId = this.env.pos.serialIndex[query];
            const product = this.env.pos.db.get_product_by_id(productId);
            if (product) {
                this.env.pos.get_order().add_product(product, { description: 'SN ${query}' });
                this.state.ui.searchWordInput = "";
                this.render();
                return;
            }
        }
        if (_superUpdateSearch) {
            return _superUpdateSearch.call(this, event);
        }
    },
});
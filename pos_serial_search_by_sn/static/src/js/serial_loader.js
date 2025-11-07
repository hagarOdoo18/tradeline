import { PosGlobalState } from "@point_of_sale/app/store/pos_global_state";
import { patch } from "@web/core/utils/patch";

const _superProcessData = PosGlobalState.prototype._processData;

patch(PosGlobalState.prototype, {
    async _processData(loadedData) {
        await _superProcessData.call(this, loadedData);

        this.serialIndex = {};
        const lots = await this.env.services.orm.searchRead(
            "stock.production.lot",
            [
                ["product_id.available_in_pos", "=", true],
                ["product_id.sale_ok", "=", true],
            ],
            ["name", "product_id"]
        );
        for (const lot of lots) {
            if (lot.name && lot.product_id && lot.product_id[0]) {
                this.serialIndex[lot.name] = lot.product_id[0];
            }
        }
    },
});
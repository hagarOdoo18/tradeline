/* Copyright (c) 2016-Present Webkul Software Pvt. Ltd. (<https://webkul.com/>) */
/* See LICENSE file for full copyright and licensing details. */
/* License URL : <https://store.webkul.com/license.html/> */

import { ProductScreen } from "@point_of_sale/app/screens/product_screen/product_screen";
import { patch } from "@web/core/utils/patch";
import { _t } from "@web/core/l10n/translation";
import { AlertDialog } from "@web/core/confirmation_dialog/confirmation_dialog";

patch(ProductScreen.prototype, {
    async _barcodeProductAction(code) {
        let product = await this._getProductByBarcode(code);    
        let skip = false;    
        if(!product){
            const data = this.pos.models['stock.lot'].find(l=>l.name == code.base_code);
            if(data){
                const product_id = data.raw.product_id;
                product = this.pos.models['product.product'].find(p=>p.id == product_id);
                if(!product){
                    await this.pos.data.read("product.product", [product_id]);
                }
                product = this.pos.models['product.product'].find(p=>p.id == product_id);
                if(!product){
                    await super._barcodeProductAction(code);
                    return;
                }
                
                let currentProductQty = 0;

                const currentOrder = this.pos.get_order();
                if(currentOrder){
                    currentProductQty = currentOrder.get_remaining_products(code.base_code);
                }

                if(product.tracking == 'serial' && currentProductQty<=0){
                    this.dialog.add(AlertDialog, {
                        title: _t("Out Of Quantity !"),
                        body: _t(`Only one product can be added by using serial number ${code.base_code}.`),
                    });
                    return;
                }

                if(product.tracking == 'lot' && currentProductQty<=0){
                    this.dialog.add(AlertDialog, {
                        title: _t("Lot Is Empty !"),
                        body: _t(`The quantity of selected product in lot ${code.base_code}. is Zero`),
                    });
                    return;
                }
                code.type='lot';
                await this.pos.addLineToCurrentOrder(
                    {product_id: product},
                    {code: code},
                    product.needToConfigure()
                )
                skip = true
                this.numberBuffer.reset();
            }
        }
        if(!skip){
            await super._barcodeProductAction(code);
        }
    }
});

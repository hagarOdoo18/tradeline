
/* Copyright (c) 2016-Present Webkul Software Pvt. Ltd. (<https://webkul.com/>) */
/* See LICENSE file for full copyright and licensing details. */
/* License URL : <https://store.webkul.com/license.html/> */

import { EditListPopup } from "@point_of_sale/app/store/select_lot_popup/select_lot_popup";
import { patch } from "@web/core/utils/patch";
import { usePos } from "@point_of_sale/app/store/pos_hook";
import { AlertDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { useService } from "@web/core/utils/hooks";
import { onMounted } from "@odoo/owl";

patch(EditListPopup.prototype,{
    setup(){
        super.setup(...arguments);
        this.pos = usePos();
        this.dialog = useService("dialog");
        onMounted(()=>{
            this.editListRef.el.addEventListener("keydown", (ev)=>this.onKeydown(ev));
        })
    },

    onKeydown(ev){
        if (ev.key && ev.key.toLowerCase() === "enter") {
            ev.preventDefault();
            this.showErrorMessage
        }
    },

    get showErrorMessage() {
        var self = this;
        const $errorMessage = document.querySelector('.error-message');    
        if (this.checkUnknowValue) {
            let lot_inputs = document.querySelectorAll('.popup-input');
            
            lot_inputs.forEach(function(lot_el){
                if(lot_el.value == self.checkUnknowValue){
                    if(!lot_el.classList.contains('is-invalid')){
                        lot_el.classList.add('is-invalid');
                        // return true;
                    }
                }
            })


            if ($errorMessage.classList.contains('d-none')) {
                $errorMessage.classList.remove('d-none')
                $errorMessage.classList.add('d-block');
            } else if ($errorMessage.classList.contains('d-block')) {
                if(document.querySelector('.select-input').value=='yes'){
                    return true;
                }
                else{
                    return false;
                }
            }

        } else {

            if(this.checkUnknowValue){
                document.querySelectorAll('.popup-input').forEach(function(idx){
                    if(idx.classList.contains('is-invalid')){
                        idx.classList.remove('is-invalid');
                    }
                })
            }
            if ($errorMessage.classList.contains('d-block')) {
                $errorMessage.classList.remove('d-block');
                $errorMessage.classList.add('d-none');
            }
        }
    },

    onUnselectItem(itemId){
        this.showErrorMessage;
        super.onUnselectItem(...arguments);
    },

    get checkUnknowValue() {
        for(const item of this.state.array){
            if(item.text && item.text!='' && !this.props.options.includes(item.text)){
                return item.text;
            }
        }
    },

    get hasNotValidValue(){
        for(const item of this.state.array){
            
            if(item.text && !this.hasValidValue(item._id, item.text)){
                return true;
            }
        }
    },

    async validateQty() {    
        const arr1Texts = new Set(this.props.array.map(item => item.text));
        const filteredArr2 = this.state.array.filter(item => !arr1Texts.has(item.text));
        if (!this.state.array.length) return true;
        const order = this.pos.get_order();
        const orderlines = order.get_orderlines();
        for (const item of filteredArr2) {
            const { text: lotName } = item;
    
            if (lotName) {
                const lot = this.pos.models['stock.lot'].find(l => l.name === lotName);
                if (!lot) continue;
                let flag = false;
                let open_orders = this.pos.get_open_orders();
                if(open_orders && open_orders.length){
                    for(const open_order of open_orders){
                        open_order.lines.some(line => 
                            line.pack_lot_ids.some(p => p.lot_name !== lotName)
                        ) && !filteredArr2.some(item2 =>
                            open_order.lines.some(line =>
                                line.pack_lot_ids.some(p => p.lot_name === item2.text)
                            )
                        );
                    }
                    
                }
                
    
                if (flag) continue;
    
                const qty = lot.product_qty;
                const quant = order.product_total_by_lot(lotName);
                const wk_product = lot.product_id;
    
                if (wk_product.tracking) {
                    if (wk_product.tracking === 'lot' && (quant > qty || qty === 0)) {
                        await this.dialog.add(AlertDialog, {
                            title: "Lot Is Empty!",
                            body: `The quantity of selected product in lot ${lotName} is Zero.`
                        });
                        return;
                    } else if (wk_product.tracking === 'serial' && quant >= qty) {
                        await this.dialog.add(AlertDialog, {
                            title: "Serial Number!",
                            body: `Only one product can be added by using serial number ${lotName}.`
                        });
                        return;
                    }
                }
            }
        }
        return true;
    },
    

    async confirm(){
        if (this.checkUnknowValue) {
            if(!this.showErrorMessage){
                this.props.close();
            }
        }
        if(this.hasNotValidValue){
            return;
        }        
        if(!await this.validateQty()){
            this.props.close();
        }
        super.confirm();
    },

    removeItem(itemId){
        this.showErrorMessage;
        super.removeItem(...arguments);
    }
})
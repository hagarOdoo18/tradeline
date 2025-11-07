odoo.define('pos_disallow_negative.paymentScreen', function (require) {
    "use strict";

    var PaymentScreen = require('point_of_sale.PaymentScreen')
    const Registries = require('point_of_sale.Registries');
    const rpc = require("web.rpc")

    const pos_disallow_negativePaymentScreen = (PaymentScreen) =>
        class extends PaymentScreen {
            async checkStock(order){
                let lines =  order.orderlines
                let products = {}
                _.each(lines, function(line){
                    let product_id = line.product.id
                    products[product_id] = line.quantity
                })
                let res = []
                await rpc.query({
                    model: 'stock.quant',
                    method: 'pos_check_quantity',
                    args: [order.pos_session_id,products],
                }).then(function (data) {
                    res = data
                });
                return res
            }

            async validateOrder(isForceValidate) {
                let order = this.currentOrder;
                let data = await this.checkStock(order);
                if (data.lines.length) {
                    this.showPopup('StockPopup', data);
                    return;
                }

                await super.validateOrder(...arguments);

            }

        }

    Registries.Component.extend(PaymentScreen, pos_disallow_negativePaymentScreen);

    return PaymentScreen;

})
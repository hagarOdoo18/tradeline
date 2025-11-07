odoo.define('isky_pos_customer_required.PaymentScreen', function (require) {
    "use strict";

    const PaymentScreen = require('point_of_sale.PaymentScreen');
    const Registries = require('point_of_sale.Registries');

    const PosCustomerRequiredPaymentScreen = (PaymentScreen) =>
        class extends PaymentScreen {
            async validateOrder(isForceValidate) {
                if(this.env.pos.config.require_customer < this.currentOrder.get_total_with_tax() && !this.currentOrder.get_client()){
                    await this.showPopup('ErrorPopup', {
                        title: this.env._t('An anonymous order cannot be confirmed'),
                        body: this.env._t('Please select a customer for this order.'),
                    });
                }
                else{
                    return super.validateOrder(...arguments);
                }
            }

        };

    Registries.Component.extend(PaymentScreen, PosCustomerRequiredPaymentScreen);

    return PaymentScreen;

});

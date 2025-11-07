odoo.define('pos_disallow_negative.StockPopup', function(require) {
    'use strict';

    const AbstractAwaitablePopup = require('point_of_sale.AbstractAwaitablePopup');
    const Registries = require('point_of_sale.Registries');
    const { useState } = owl

    class StockPopup extends AbstractAwaitablePopup {
        setup() {
            super.setup();
            this.state = useState({
                lines: _.map(this.props.lines, function(line,index){
                    return {id:index+1,name:line[0],demanded_qty:line[1],available_qty:line[2]}
                }),
                location: this.props.location
            });
            console.log("state", this.state)
        }

        async confirm() {
            super.confirm()
        }

    }

    StockPopup.template = 'StockPopup';
    Registries.Component.add(StockPopup);

    return StockPopup;
});

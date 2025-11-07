odoo.define("sh_pos_counter.pos", function (require) {
    "use strict";

    var models = require("point_of_sale.models");
    var core = require("web.core");
    var QWeb = core.qweb;
    var _t = core._t;
    var rpc = require('web.rpc');


    var _super_Order = models.Order.prototype;
    models.Order = models.Order.extend({
        export_for_printing: function () {
            var receipt = _super_Order.export_for_printing.apply(this, arguments);
            var order = this.pos.get_order();
            var self = this
            var uuid=this.pos.validated_orders_name_server_id_map
            const orderName = order.get_name();
            const order_server_id = this.pos.validated_orders_name_server_id_map[orderName];
            const url = this.pos.validated_orders_name_server_id_map[order_server_id];
            receipt["url"] = url;
            return receipt;
        },
    });

});

odoo.define("eg_eta_receipt.models", function (require) {
    "use strict";

    var models = require("point_of_sale.models");
    var rpc = require("web.rpc");


    var _super_posmodel = models.PosModel.prototype;
    models.PosModel = models.PosModel.extend({
        _flush_orders: function (orders, options) {
            var self = this;
            this.set_synch("connecting", orders.length);
            return this._save_to_server(orders, options)
                .then(function (server_ids) {
                    self.set_synch("connected");
                    for (let i = 0; i < server_ids.length; i++) {
                        self.validated_orders_name_server_id_map[server_ids[i].pos_reference] = server_ids[i].id;
                        self.validated_orders_name_server_id_map[server_ids[i].id] = server_ids[i].electronic_invoice_url;
                    }
                    return _.pluck(server_ids, 'id');
        }).catch(function(error){
            self.set_synch(self.get('failed') ? 'error' : 'disconnected');
            throw error;
        }).finally(function() {
            self._after_flush_orders(orders);
        });
        },

    });
});

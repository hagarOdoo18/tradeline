odoo.define('pos_serial_search.models', function (require) {
    "use strict";

    const models = require('point_of_sale.models');

    const _super_product = models.Product.prototype;
    models.Product = models.Product.extend({
        initialize: function (attributes, options) {
            _super_product.initialize.apply(this, arguments);
            this.lot_names = this.lot_names || [];
        },
        matches: function (search) {
            const res = _super_product.matches.apply(this, arguments);
            if (res) {
                return true;
            }
            const query = search.toLowerCase();
            if (this.lot_names && this.lot_names.some(l => l.toLowerCase().includes(query))) {
                return true;
            }
            return false;
        },
    });
});

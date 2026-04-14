/** @odoo-module **/
import { registry } from "@web/core/registry";
import { Layout } from "@web/search/layout";
import { _t } from "@web/core/l10n/translation";
import { useService } from "@web/core/utils/hooks";
import { rpc } from "@web/core/network/rpc";
import { onWillStart, useState, onMounted, Component } from "@odoo/owl";
/**
 * Class representing the Shopify Dashboard.
 * This class extends the Component class from the Odoo Owl framework.
 */
export class ShopifyDashboard extends Component {

async setup() {
        super.setup(...arguments);
        // Initialize services and component state
        this.initial_render = true;
        this.rpc = rpc;
        this.action = useService('action');
        this.state = useState({
            outputs: [],
            customer: 0,

          product: 0,
            order: 0,
        });
         onWillStart(async () => {
            const self = this;
            // Fetch total order, customer, and product data
            const result = await this.rpc('/total_dashboard', {});
            const { customer, product, order } = result[0];
            self.state.customer = customer;
            self.state.product = product;
            self.state.order = order;
            // Fetch dashboard data
            self.state.outputs = await this.rpc('/dashboard', {});
        });
//        });
        // Define behavior to execute after rendering
        onMounted(() => {
            this.state.outputs.forEach(function(rec) {
                    google.charts.load('current', {
                        'packages': ['corechart']
                    });
                    google.charts.setOnLoadCallback(drawChart);
                    // Function to draw a chart
                    function drawChart() {
                        var data = google.visualization.arrayToDataTable([
                            ["", "", {
                                role: "style"
                            }],
                            ["Customers", rec['customer'], "#b87333"],
                            ["Products", rec['product'], "silver"],
                            ["Orders", rec['order'], "gold"]
                        ]);
                        var options = {
                            title: rec['instance'],
                            titleTextStyle: {
                                fontSize: 15,
                                bold: true
                            }
                        };
                        // Check if chart container element exists
                        if (document.getElementById(rec['consumer_key'])) {
                            var chart = new google.visualization.ColumnChart(document.getElementById(rec['consumer_key']));
                            chart.draw(data, options);
                        } else {
                            console.warn('Chart container not found for:', rec['consumer_key']);
                        }
                    }
                });
        });
    }
    /**
     * Event handler for viewing customer details.
     * @param {Event} e - The event object.
     */
    view_customer(e) {
        // Options for the action to be performed
        var options = {
            on_reverse_breadcrumb: self.on_reverse_breadcrumb,
        };
        // Perform the action to view customer details
        this.env.services.action.doAction({
            name: _t("Customers"),
            type: 'ir.actions.act_window',
            res_model: 'res.partner',
            view_mode: 'list,form',
            views: [[false, 'list'], [false, 'form']],
            domain: [['shopify_sync_ids', '!=', false]],
            target: 'current'
        }, options);
    }
    /**
     * Event handler for viewing product details.
     * @param {Event} e - The event object.
     */
    view_products(e) {
//        e.preventDefault();
        // Options for the action to be performed
        var options = {
            on_reverse_breadcrumb: self.on_reverse_breadcrumb,
        };
        // Perform the action to view product details
        this.env.services.action.doAction({
            name: _t("Products"),
            type: 'ir.actions.act_window',
            res_model: 'product.template',
            view_mode: 'list,form',
            views: [[false, 'list'], [false, 'form']],
            domain: [['shopify_sync_ids', '!=', false]],
            target: 'current'
        }, options);
    }
    /**
     * Event handler for viewing order details.
     * @param {Event} e - The event object.
     */
    view_orders(e) {
//        e.stopPropagation();
//        e.preventDefault();
        // Options for the action to be performed
        var options = {
            on_reverse_breadcrumb: self.on_reverse_breadcrumb,
        };
        // Perform the action to view order details
        this.env.services.action.doAction({
            name: _t("Sale Orders"),
            type: 'ir.actions.act_window',
            res_model: 'sale.order',
            view_mode: 'list,form',
            views: [[false, 'list'], [false, 'form']],
            domain: [['shopify_sync_ids', '!=', false]],
            target: 'current'
        }, options);
    }
}
// Define the template for the ShopifyDashboard class
ShopifyDashboard.template = "ShopifyDashboard";
// Define the components used by the ShopifyDashboard class
ShopifyDashboard.components = { Layout };
// Register the ShopifyDashboard class under the "actions" category in the
//Odoo registry
registry.category("actions").add("shopify_dashboard", ShopifyDashboard);
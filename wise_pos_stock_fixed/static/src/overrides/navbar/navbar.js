/** @odoo-module */
import { patch } from '@web/core/utils/patch';
import { Navbar } from '@point_of_sale/app/navbar/navbar';

patch(Navbar.prototype, {
  async onStockListClick() {
    //TBD : Why call same function from service
    //await this.env.services.pos.showTempScreen(
    console.log(this,"...............thispos");
    await this.pos.showScreen('StockListScreen');
    //const { confirmed } = await this.pos.showTempScreen('StockListScreen');
    // if (confirmed) {
    //   currentOrder.set_partner(newPartner);
    // }
  },
});

/** @odoo-module */
export function getProductQty(stockType, product) {
  const qty =
    stockType == 'on_hand' ? product.qty_available : product.virtual_available;
  return qty;
}

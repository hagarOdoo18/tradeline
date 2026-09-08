# -*- coding: utf-8 -*-
################################################################################
#
#    Cybrosys Technologies Pvt. Ltd.
#
#    Copyright (C) 2025-TODAY Cybrosys Technologies(<https://www.cybrosys.com>).
#    Author: Cybrosys Techno Solutions (Contact : odoo@cybrosys.com)
#
#    This program is under the terms of the Odoo Proprietary License v1.0
#    (OPL-1)
#    It is forbidden to publish, distribute, sublicense, or sell copies of the
#    Software or modified copies of the Software.
#
#    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
#    IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
#    DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
#    OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE
#    USE OR OTHER DEALINGS IN THE SOFTWARE.
#
################################################################################
import logging
from odoo import models

_logger = logging.getLogger(__name__)

# Number of groups queued per job.cron batch (matches sync.inventory).
INVENTORY_BATCH_SIZE = 20

# Picking types whose validation changes the sellable stock we mirror to
# Shopify: deliveries take stock out, receipts (and customer returns, which
# are incoming) put it back in. Internal transfers are deliberately excluded —
# they move stock between locations of the same warehouse, and the quantity
# read for Shopify is the warehouse's lot_stock_id total, which does not
# change. A transfer to a *different* mapped warehouse is still picked up by
# the incremental cron.
SYNCED_PICKING_TYPE_CODES = ('outgoing', 'incoming')


class StockPicking(models.Model):
    """Push Odoo on-hand quantities to Shopify when a picking is validated."""
    _inherit = 'stock.picking'

    def button_validate(self):
        """Override: after a picking is validated, queue a Shopify inventory
        sync for every product on the picking that is linked to Shopify.

        The super() call runs first so stock moves are actually processed and
        on-hand quantities are up to date before we read them. The sync itself
        is queued as job.cron records and processed asynchronously by
        JobCron._do_job, so a slow or failing Shopify API never blocks or
        breaks the validation."""
        res = super().button_validate()
        for picking in self:
            # button_validate may return an action (backorder / immediate
            # transfer wizard); in that case the picking is not 'done' yet and
            # this guard skips it until validation actually completes.
            if picking.state != 'done':
                continue
            try:
                picking._sync_shopify_inventory_on_validate()
            except Exception:
                _logger.exception(
                    'Shopify inventory sync could not be queued after '
                    'validating picking %s', picking.name)
        return res

    def _sync_shopify_inventory_on_validate(self):
        """Queue export_inventory_to_shopify job.cron records for the Shopify
        variants matching the products on this picking.

        Runs for deliveries and receipts alike (SYNCED_PICKING_TYPE_CODES):
        both change the warehouse on-hand quantity Shopify is told about, so a
        receipt has to publish the stock going *up* just as a delivery
        publishes it going down.

        The products are grouped by barcode / shopify_variant_sku first (see
        SyncInventory._build_inventory_groups), so moving stock of one variant
        also refreshes the sibling variants that share the same code."""
        self.ensure_one()

        if self.picking_type_code not in SYNCED_PICKING_TYPE_CODES:
            return

        products = self.move_ids.mapped('product_id')
        if not products:
            return

        model = self.env['ir.model'].sudo().search(
            [('model', '=', 'sync.inventory')], limit=1)
        if not model:
            return

        instances = self.env['shopify.configuration'].sudo().search([
            ('company_id', '=', self.company_id.id),
            ('active', '=', True),
        ])

        for instance in instances:
            # Warehouses mapped to an active Shopify location for this instance.
            warehouse_ids = self.env['shopify.location'].sudo().search([
                ('instance_id', '=', instance.id),
                ('warehouse_id', '=', self.picking_type_id.warehouse_id.id),
                ('active', '=', True),
            ]).mapped('warehouse_id').ids
            if not warehouse_ids:
                continue

            # Groups (barcode / shopify_variant_sku) for the products on this
            # picking, completed with the variants sharing the same code.
            groups = self.env['sync.inventory'].sudo()._build_inventory_groups(
                instance, products=products)
            if not groups:
                continue

            for i in range(0, len(groups), INVENTORY_BATCH_SIZE):
                self.env['job.cron'].sudo().create([{
                    'model_id': model.id,
                    'function': 'export_inventory_to_shopify',
                    'data': {
                        'groups': groups[i:i + INVENTORY_BATCH_SIZE],
                        'warehouse_ids': warehouse_ids,
                    },
                    'instance_id': instance.id,
                }])

            _logger.info(
                'Shopify inventory sync: queued %d group(s) covering %d '
                'variant(s) for instance %s after validating %s picking %s',
                len(groups),
                sum(len(group['variant_ids']) for group in groups),
                instance.name, self.picking_type_code, self.name)

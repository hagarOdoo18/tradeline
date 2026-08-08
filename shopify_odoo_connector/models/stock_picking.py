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

# Number of variants queued per job.cron batch (matches sync.inventory).
INVENTORY_BATCH_SIZE = 20


class StockPicking(models.Model):
    """Push Odoo on-hand quantities to Shopify when a delivery is validated."""
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
        variants matching the products on this (delivery) picking."""
        self.ensure_one()

        # Only outgoing deliveries reduce sellable stock we mirror to Shopify.
        if self.picking_type_code != 'outgoing':
            return

        product_ids = self.move_ids.mapped('product_id').ids
        if not product_ids:
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

            # Synced variants for just the products on this picking.
            sync_ids = self.env['shopify.sync'].sudo().search([
                ('instance_id', '=', instance.id),
                ('shopify_variant_id', '!=', False),
                ('product_prod_id', 'in', product_ids),
            ]).ids
            if not sync_ids:
                continue

            for i in range(0, len(sync_ids), INVENTORY_BATCH_SIZE):
                self.env['job.cron'].sudo().create([{
                    'model_id': model.id,
                    'function': 'export_inventory_to_shopify',
                    'data': {
                        'sync_ids': sync_ids[i:i + INVENTORY_BATCH_SIZE],
                        'warehouse_ids': warehouse_ids,
                    },
                    'instance_id': instance.id,
                }])

            _logger.info(
                'Shopify inventory sync: queued %d variant(s) for instance %s '
                'after validating delivery %s',
                len(sync_ids), instance.name, self.name)

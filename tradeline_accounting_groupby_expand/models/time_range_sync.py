from odoo import api, models

from ..hooks import sync_native_time_ranges


class TradelineAccountingGroupbyExpandSync(models.AbstractModel):
    _name = "tradeline.accounting.groupby.expand.sync"
    _description = "Tradeline Accounting GroupBy Expand Sync"

    @api.model
    def run(self):
        sync_native_time_ranges(self.env)
        return True

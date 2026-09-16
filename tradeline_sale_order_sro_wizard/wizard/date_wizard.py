from datetime import datetime, time, timedelta

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class SaleOrderSROWizard(models.TransientModel):
    _name = "sale.order.sro.wizard"
    _description = "Sale Order SRO Date Range Wizard"

    start_date = fields.Date(string="Start Date", required=True)
    end_date = fields.Date(string="End Date", required=True)

    @api.constrains("start_date", "end_date")
    def _check_dates(self):
        for wizard in self:
            if wizard.start_date and wizard.end_date and wizard.start_date > wizard.end_date:
                raise ValidationError(_("Start Date cannot be after End Date."))

    def action_open_sro_orders_in_range(self):
        self.ensure_one()

        start_dt = fields.Datetime.to_string(datetime.combine(self.start_date, time.min))
        end_exclusive = datetime.combine(self.end_date + timedelta(days=1), time.min)
        end_dt = fields.Datetime.to_string(end_exclusive)

        return {
            "name": _("SRO In Range"),
            "type": "ir.actions.act_window",
            "res_model": "sale.order",
            "view_mode": "list,form,pivot,graph",
            "domain": [
                ("create_date", ">=", start_dt),
                ("create_date", "<", end_dt),
                ("inv_type", "=", "sro"),
            ],
            "context": {"search_default_my_quotation": 0},
        }


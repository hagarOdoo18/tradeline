# -*- coding: utf-8 -*-
from odoo import models
from odoo.tools import float_is_zero


class ProductProduct(models.Model):
    _inherit = 'product.product'

    def _sync_standard_price_from_valuation(self, company):
        """Align AVCO Product Cost after a valuation-only quantity correction.

        Writing ``standard_price`` through the ORM creates a new revaluation layer.
        That is correct for a deliberate revaluation, but wrong after we have just
        repaired the quantities/values of existing valuation layers.  In that case
        the valuation already contains the authoritative total, so update only the
        company-dependent cost cache without creating another valuation movement.
        """
        self.ensure_one()
        product = self.with_company(company).sudo()
        if product.categ_id.property_cost_method != 'average':
            return None

        self.env.cr.execute(
            """
                SELECT
                    COALESCE(SUM(quantity), 0.0),
                    COALESCE(SUM(value), 0.0)
                FROM stock_valuation_layer
                WHERE product_id = %s
                  AND company_id = %s
            """,
            (self.id, company.id),
        )
        valuation_qty, valuation_value = self.env.cr.fetchone()
        if float_is_zero(valuation_qty, precision_rounding=product.uom_id.rounding):
            return None

        unit_cost = valuation_value / valuation_qty
        self.env.cr.execute(
            """
                UPDATE product_product
                   SET standard_price = jsonb_set(
                           COALESCE(standard_price, '{}'::jsonb),
                           ARRAY[%s]::text[],
                           to_jsonb(%s::double precision),
                           TRUE
                       ),
                       write_uid = %s,
                       write_date = NOW()
                 WHERE id = %s
            """,
            (str(company.id), unit_cost, self.env.user.id, self.id),
        )
        self.invalidate_recordset(['standard_price'])
        return unit_cost

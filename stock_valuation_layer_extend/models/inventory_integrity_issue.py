from odoo import _, fields, models, tools
from odoo.exceptions import UserError


class InventoryIntegrityIssue(models.Model):
    _name = 'stock.inventory.integrity.issue'
    _description = 'Inventory Integrity Issue'
    _auto = False
    _rec_name = 'product_id'
    _order = 'company_id, product_id, lot_id'

    company_id = fields.Many2one('res.company', readonly=True)
    currency_id = fields.Many2one('res.currency', readonly=True)
    product_id = fields.Many2one('product.product', readonly=True)
    product_category_id = fields.Many2one('product.category', string='Category', readonly=True)
    product_family_id = fields.Many2one('product.family', string='Family', readonly=True)
    lot_id = fields.Many2one('stock.lot', readonly=True)
    physical_quantity = fields.Float(readonly=True, digits='Product Unit of Measure')
    valuation_quantity = fields.Float(readonly=True, digits='Product Unit of Measure')
    quantity_gap = fields.Float(readonly=True, digits='Product Unit of Measure')
    valuation_value = fields.Monetary(readonly=True)
    stored_cost = fields.Float(readonly=True, digits='Product Price')
    valuation_cost = fields.Float(readonly=True, digits='Product Price')
    issue = fields.Char(readonly=True)

    def action_reconcile_valuation(self):
        self.ensure_one()
        if self.company_id != self.env.company:
            raise UserError(_('Switch to this issue’s company first.'))
        if not self.env.user.has_group('stock_valuation_layer_extend.group_stock_valuation_quantity_correction'):
            raise UserError(_('A valuation reconciliation manager is required.'))
        wizard = self.env['stock.valuation.reconciliation.wizard'].create({
            'company_id': self.company_id.id, 'product_id': self.product_id.id,
            'reason': self.issue or _('Administrator valuation review'),
        })
        current = wizard._read_balances(self.product_id)
        wizard.write({'physical_quantity': current['physical'], 'valuation_quantity': current['quantity'],
                      'valuation_value': current['value'],
                      'correction_quantity': current['physical'] - current['quantity']})
        return {'type': 'ir.actions.act_window', 'name': _('Reconcile Valuation to Verified Stock'),
                'res_model': 'stock.valuation.reconciliation.wizard', 'view_mode': 'form', 'target': 'new',
                'res_id': wizard.id}

    def action_adjust(self):
        # Inventory Issues repairs valuation/cost, never the physical stock count.
        return self.action_reconcile_valuation()

    def action_count_stock(self):
        self.ensure_one()
        if self.company_id != self.env.company:
            raise UserError(_('Switch to this issue’s company first.'))
        quants = self.env['stock.quant'].search([
            ('company_id', '=', self.company_id.id), ('product_id', '=', self.product_id.id),
            ('lot_id', '=', self.lot_id.id or False), ('location_id.usage', '=', 'internal'),
            ('owner_id', '=', False), ('quantity', '>', 0)])
        defaults = {'default_product_id': self.product_id.id, 'default_lot_id': self.lot_id.id,
                    'default_reason': self.issue}
        if len(quants) == 1:
            defaults.update(default_location_id=quants.location_id.id, default_counted_quantity=quants.quantity)
        return {'type': 'ir.actions.act_window', 'name': _('Review Count & Cost'),
                'res_model': 'stock.count.cost.wizard', 'view_mode': 'form', 'target': 'new', 'context': defaults}

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute(f"""
            CREATE VIEW {self._table} AS
            WITH physical AS (
                SELECT q.company_id, q.product_id, q.lot_id,
                       SUM(q.quantity) AS qty, BOOL_OR(q.quantity < 0) AS negative,
                       COUNT(*) FILTER (WHERE q.quantity > 0) AS positions
                FROM stock_quant q JOIN stock_location l ON l.id = q.location_id
                WHERE q.owner_id IS NULL AND l.usage IN ('internal', 'transit')
                GROUP BY q.company_id, q.product_id, q.lot_id
            ), valued AS (
                SELECT company_id, product_id, lot_id, SUM(quantity) AS qty, SUM(value) AS value
                FROM stock_valuation_layer GROUP BY company_id, product_id, lot_id
            ), serial_balances AS (
                SELECT COALESCE(p.company_id,v.company_id) AS company_id,
                       COALESCE(p.product_id,v.product_id) AS product_id,
                       COALESCE(p.lot_id,v.lot_id) AS lot_id,
                       COALESCE(p.qty,0) AS physical_qty, COALESCE(v.qty,0) AS valued_qty,
                       COALESCE(v.value,0) AS value, COALESCE(p.negative,FALSE) AS negative,
                       COALESCE(p.positions,0) AS positions
                FROM physical p FULL JOIN valued v ON p.company_id=v.company_id
                    AND p.product_id=v.product_id AND p.lot_id IS NOT DISTINCT FROM v.lot_id
            ), balances AS (
                SELECT b.*, COALESCE((lot.standard_price->>b.company_id::text)::float,0) AS cost
                FROM serial_balances b JOIN stock_lot lot ON lot.id=b.lot_id
                JOIN product_product lp ON lp.id=b.product_id
                JOIN product_template lt ON lt.id=lp.product_tmpl_id WHERE lt.lot_valuated
                UNION ALL
                SELECT b.company_id,b.product_id,NULL::integer,SUM(b.physical_qty),SUM(b.valued_qty),
                       SUM(b.value),BOOL_OR(b.negative),0,
                       COALESCE((pp.standard_price->>b.company_id::text)::float,0)
                FROM serial_balances b JOIN product_product pp ON pp.id=b.product_id
                GROUP BY b.company_id,b.product_id,pp.standard_price
            ), flagged AS (
                SELECT b.*, company.currency_id,
                    CONCAT_WS('; ',
                        CASE WHEN ABS(b.physical_qty-b.valued_qty) >= uom.rounding/2 THEN 'Physical / valuation quantity mismatch' END,
                        CASE WHEN b.negative THEN 'Negative physical stock' END,
                        CASE WHEN b.lot_id IS NOT NULL AND pt.tracking='serial' AND (b.physical_qty>1 OR b.positions>1) THEN 'Duplicate serial stock' END,
                        CASE WHEN ABS(b.valued_qty)<uom.rounding/2 AND ABS(b.value)>=currency.rounding/2 THEN 'Value without stock' END,
                        CASE WHEN b.valued_qty>0 AND b.value<=0 THEN 'Zero or negative inventory value' END,
                        CASE WHEN b.valued_qty>0 AND b.cost<=0 THEN 'Zero or negative stored cost' END,
                        CASE WHEN cat.property_cost_method->>b.company_id::text='average' AND b.valued_qty>0
                            AND ABS(b.cost-b.value/b.valued_qty)>=currency.rounding/2 THEN 'Stored cost differs from valuation average' END
                    ) AS issue
                FROM balances b JOIN product_product pp ON pp.id=b.product_id
                JOIN product_template pt ON pt.id=pp.product_tmpl_id
                JOIN product_category cat ON cat.id=pt.categ_id
                JOIN uom_uom uom ON uom.id=pt.uom_id
                JOIN res_company company ON company.id=b.company_id
                JOIN res_currency currency ON currency.id=company.currency_id
            )
            SELECT CASE WHEN lot_id IS NULL THEN -((flagged.company_id::bigint<<32)+flagged.product_id)
                        ELSE (flagged.company_id::bigint<<32)+lot_id END AS id,
                   flagged.company_id,flagged.currency_id,flagged.product_id,flagged.lot_id,
                   pt.categ_id AS product_category_id, pt.family_id AS product_family_id,
                   physical_qty AS physical_quantity,
                   valued_qty AS valuation_quantity,physical_qty-valued_qty AS quantity_gap,
                   value AS valuation_value,cost AS stored_cost,
                   CASE WHEN valued_qty>0 THEN value/valued_qty ELSE 0 END AS valuation_cost,issue
            FROM flagged JOIN product_product pp ON pp.id=flagged.product_id
            JOIN product_template pt ON pt.id=pp.product_tmpl_id WHERE issue<>''
        """)

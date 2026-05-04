# -*- coding: utf-8 -*-
from odoo import fields, models, tools


class PaymentReportLine(models.Model):
    """Payment Report Line — DIAGNOSTIC build.

    The SQL view currently returns 5 hard-coded rows so we can verify the
    list renders independently of the real reconciliation query. If this
    version shows rows without the Owl error, the previous failures were
    in the dynamic SQL (NULL data, type mismatch, missing columns). If
    this still errors, the cause is in the model/view layer (bad cached
    asset bundle, leftover saved Group By chip on a removed column,
    persistent menu state pointing at a stale view, etc.).
    """
    _name = 'payment.report.line'
    _description = 'Payment Report Line'
    _auto = False
    _log_access = False
    _order = 'date desc, id desc'

    branch_name = fields.Char(string='Branch', readonly=True)
    date = fields.Date(string='Date', readonly=True)
    invoice_name = fields.Char(string='Invoice', readonly=True)
    sale_order_name = fields.Char(string='Sale Order', readonly=True)
    partner_name = fields.Char(string='Partner', readonly=True)
    journal_name = fields.Char(string='Journal / Method', readonly=True)
    source_type = fields.Selection(
        selection=[
            ('invoice', 'Invoice'),
            ('invoice_pos', 'Invoice (POS)'),
            ('credit', 'Credit Note'),
            ('credit_pos', 'Credit Note (POS)'),
            ('order_payment', 'Order Payment'),
        ],
        string='Source',
        readonly=True,
    )
    amount = fields.Float(string='Amount', readonly=True, digits=(16, 2))

    def init(self):
        cr = self.env.cr
        cr.execute("SELECT relkind FROM pg_class WHERE relname = %s", (self._table,))
        row = cr.fetchone()
        if row:
            kind = row[0]
            if kind == 'r':
                cr.execute(f'DROP TABLE "{self._table}" CASCADE')
            elif kind == 'v':
                cr.execute(f'DROP VIEW "{self._table}" CASCADE')
            elif kind == 'm':
                cr.execute(f'DROP MATERIALIZED VIEW "{self._table}" CASCADE')

        # Hard-coded 5 rows — proves rendering works regardless of real data.
        cr.execute(f"""
            CREATE OR REPLACE VIEW {self._table} AS (
                SELECT
                    n::integer                                     AS id,
                    ('Branch ' || n)::varchar                      AS branch_name,
                    (CURRENT_DATE - (n * INTERVAL '1 day'))::date  AS date,
                    ('INV/0000' || n)::varchar                     AS invoice_name,
                    ''::varchar                                    AS sale_order_name,
                    ('Partner ' || n)::varchar                     AS partner_name,
                    'Bank'::varchar                                AS journal_name,
                    CASE WHEN n % 2 = 0
                         THEN 'invoice'::varchar
                         ELSE 'credit'::varchar END                AS source_type,
                    (n * 100.0)::numeric                           AS amount
                FROM generate_series(1, 5) AS n
            )
        """)

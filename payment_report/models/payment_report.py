# -*- coding: utf-8 -*-
from odoo import api, fields, models


class PaymentReportLine(models.Model):
    """Payment Report Line — backed by a Postgres view (`_auto = False`).

    `init()` (re)creates the SQL view on module install/upgrade. Every
    list/pivot/graph query is a single Postgres query — no Python loop,
    no N+1, no cache.

    The view UNIONs five sources (mirroring the original Python loop):
      1. Posted out_invoices reconciled to an account.payment
      2. Posted out_invoices invoiced via POS (no account.payment)
      3. Posted out_refunds reconciled to an account.payment (negative amount)
      4. Posted out_refunds via POS (positive amount)
      5. account.payment with sale_order_id, state='paid'
    """
    _name = 'payment.report.line'
    _description = 'Payment Report Line'
    _auto = False
    _order = 'date desc, id desc'

    branch_id = fields.Many2one('res.branch', string='Branch', readonly=True)
    date = fields.Date(string='Date', readonly=True)
    move_id = fields.Many2one('account.move', string='Invoice / Credit Note', readonly=True)
    invoice_name = fields.Char(string='Invoice', readonly=True)
    sale_order_id = fields.Many2one('sale.order', string='Sale Order Ref', readonly=True)
    sale_order_name = fields.Char(string='Sale Order', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Partner', readonly=True)
    journal_name = fields.Char(string='Journal / Method', readonly=True)
    amount = fields.Float(string='Amount', readonly=True, digits=(16, 2))
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

    # ----------------------------------------------------------------
    # SQL view
    # ----------------------------------------------------------------
    def _has_column(self, table, column):
        self.env.cr.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = %s AND column_name = %s",
            (table, column),
        )
        return bool(self.env.cr.fetchone())

    def _has_table(self, table):
        self.env.cr.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
            (table,),
        )
        return bool(self.env.cr.fetchone())

    def init(self):
        cr = self.env.cr
        # Drop whatever object currently holds the name (table, view, mat. view).
        cr.execute("SELECT relkind FROM pg_class WHERE relname = %s", (self._table,))
        row = cr.fetchone()
        if row:
            relkind = row[0]
            if relkind == 'r':
                cr.execute(f'DROP TABLE "{self._table}" CASCADE')
            elif relkind == 'v':
                cr.execute(f'DROP VIEW "{self._table}" CASCADE')
            elif relkind == 'm':
                cr.execute(f'DROP MATERIALIZED VIEW "{self._table}" CASCADE')

        has_move_branch = self._has_column('account_move', 'branch_id')
        has_pay_branch = self._has_column('account_payment', 'branch_id')
        has_pay_so = self._has_column('account_payment', 'sale_order_id')
        has_pos = self._has_table('pos_order') and self._has_table('pos_payment')

        move_branch = "inv.branch_id" if has_move_branch else "NULL::integer"
        pay_branch = "pay.branch_id" if has_pay_branch else "NULL::integer"

        # 10 columns per branch, in the same order as the model fields.
        sql_invoice_payment = f"""
            SELECT
                {move_branch}                                AS branch_id,
                inv.invoice_date                             AS date,
                inv.id                                       AS move_id,
                inv.name                                     AS invoice_name,
                NULL::integer                                AS sale_order_id,
                NULL::varchar                                AS sale_order_name,
                inv.partner_id                               AS partner_id,
                j.name                                       AS journal_name,
                pay.amount                                   AS amount,
                'invoice'::varchar                           AS source_type
            FROM account_move inv
            JOIN account_move_line inv_aml ON inv_aml.move_id = inv.id
            JOIN account_account aa        ON aa.id = inv_aml.account_id
                                          AND aa.account_type = 'asset_receivable'
            JOIN account_partial_reconcile pr ON pr.debit_move_id  = inv_aml.id
                                              OR pr.credit_move_id = inv_aml.id
            JOIN account_move_line pay_aml ON pay_aml.id = (
                CASE WHEN pr.debit_move_id = inv_aml.id
                     THEN pr.credit_move_id ELSE pr.debit_move_id END
            )
            JOIN account_payment pay   ON pay.move_id = pay_aml.move_id
            JOIN account_journal j     ON j.id = pay.journal_id
            WHERE inv.move_type = 'out_invoice'
              AND inv.state = 'posted'
        """

        sql_invoice_pos = f"""
            SELECT
                {move_branch}                                AS branch_id,
                inv.invoice_date                             AS date,
                inv.id                                       AS move_id,
                inv.name                                     AS invoice_name,
                NULL::integer                                AS sale_order_id,
                NULL::varchar                                AS sale_order_name,
                inv.partner_id                               AS partner_id,
                ppm.name                                     AS journal_name,
                pp.amount                                    AS amount,
                'invoice_pos'::varchar                       AS source_type
            FROM account_move inv
            JOIN pos_order po           ON po.account_move = inv.id
            JOIN pos_payment pp         ON pp.pos_order_id = po.id
            JOIN pos_payment_method ppm ON ppm.id = pp.payment_method_id
            WHERE inv.move_type = 'out_invoice'
              AND inv.state = 'posted'
              AND NOT EXISTS (
                  SELECT 1
                  FROM account_move_line aml
                  JOIN account_account aa ON aa.id = aml.account_id
                                         AND aa.account_type = 'asset_receivable'
                  JOIN account_partial_reconcile pr2 ON pr2.debit_move_id  = aml.id
                                                     OR pr2.credit_move_id = aml.id
                  JOIN account_move_line pay_aml2 ON pay_aml2.id = (
                      CASE WHEN pr2.debit_move_id = aml.id
                           THEN pr2.credit_move_id ELSE pr2.debit_move_id END
                  )
                  JOIN account_payment pay2 ON pay2.move_id = pay_aml2.move_id
                  WHERE aml.move_id = inv.id
              )
        """

        sql_credit_payment = f"""
            SELECT
                {move_branch}                                AS branch_id,
                inv.invoice_date                             AS date,
                inv.id                                       AS move_id,
                inv.name                                     AS invoice_name,
                NULL::integer                                AS sale_order_id,
                NULL::varchar                                AS sale_order_name,
                inv.partner_id                               AS partner_id,
                j.name                                       AS journal_name,
                -1 * pay.amount                              AS amount,
                'credit'::varchar                            AS source_type
            FROM account_move inv
            JOIN account_move_line inv_aml ON inv_aml.move_id = inv.id
            JOIN account_account aa        ON aa.id = inv_aml.account_id
                                          AND aa.account_type = 'asset_receivable'
            JOIN account_partial_reconcile pr ON pr.debit_move_id  = inv_aml.id
                                              OR pr.credit_move_id = inv_aml.id
            JOIN account_move_line pay_aml ON pay_aml.id = (
                CASE WHEN pr.debit_move_id = inv_aml.id
                     THEN pr.credit_move_id ELSE pr.debit_move_id END
            )
            JOIN account_payment pay   ON pay.move_id = pay_aml.move_id
            JOIN account_journal j     ON j.id = pay.journal_id
            WHERE inv.move_type = 'out_refund'
              AND inv.state = 'posted'
        """

        sql_credit_pos = f"""
            SELECT
                {move_branch}                                AS branch_id,
                inv.invoice_date                             AS date,
                inv.id                                       AS move_id,
                inv.name                                     AS invoice_name,
                NULL::integer                                AS sale_order_id,
                NULL::varchar                                AS sale_order_name,
                inv.partner_id                               AS partner_id,
                ppm.name                                     AS journal_name,
                pp.amount                                    AS amount,
                'credit_pos'::varchar                        AS source_type
            FROM account_move inv
            JOIN pos_order po           ON po.account_move = inv.id
            JOIN pos_payment pp         ON pp.pos_order_id = po.id
            JOIN pos_payment_method ppm ON ppm.id = pp.payment_method_id
            WHERE inv.move_type = 'out_refund'
              AND inv.state = 'posted'
              AND NOT EXISTS (
                  SELECT 1
                  FROM account_move_line aml
                  JOIN account_account aa ON aa.id = aml.account_id
                                         AND aa.account_type = 'asset_receivable'
                  JOIN account_partial_reconcile pr2 ON pr2.debit_move_id  = aml.id
                                                     OR pr2.credit_move_id = aml.id
                  JOIN account_move_line pay_aml2 ON pay_aml2.id = (
                      CASE WHEN pr2.debit_move_id = aml.id
                           THEN pr2.credit_move_id ELSE pr2.debit_move_id END
                  )
                  JOIN account_payment pay2 ON pay2.move_id = pay_aml2.move_id
                  WHERE aml.move_id = inv.id
              )
        """

        sql_order_payment = f"""
            SELECT
                {pay_branch}                                 AS branch_id,
                pay.date                                     AS date,
                NULL::integer                                AS move_id,
                NULL::varchar                                AS invoice_name,
                so.id                                        AS sale_order_id,
                so.name                                      AS sale_order_name,
                so.partner_id                                AS partner_id,
                j.name                                       AS journal_name,
                CASE WHEN pay.payment_type = 'outbound'
                     THEN -1 * pay.amount ELSE pay.amount END AS amount,
                'order_payment'::varchar                     AS source_type
            FROM account_payment pay
            JOIN sale_order so      ON so.id = pay.sale_order_id
            JOIN account_journal j  ON j.id  = pay.journal_id
            WHERE pay.state = 'paid'
              AND pay.sale_order_id IS NOT NULL
        """

        parts = [sql_invoice_payment, sql_credit_payment]
        if has_pos:
            parts.append(sql_invoice_pos)
            parts.append(sql_credit_pos)
        if has_pay_so:
            parts.append(sql_order_payment)

        body = "\n            UNION ALL\n            ".join(p.strip() for p in parts)
        cr.execute(f"""
            CREATE OR REPLACE VIEW {self._table} AS (
                SELECT (ROW_NUMBER() OVER ())::integer AS id, sub.*
                FROM (
                    {body}
                ) sub
            )
        """)

    # ----------------------------------------------------------------
    # Row action
    # ----------------------------------------------------------------
    def action_open_source(self):
        self.ensure_one()
        if self.move_id:
            return {
                'type': 'ir.actions.act_window',
                'res_model': 'account.move',
                'res_id': self.move_id.id,
                'view_mode': 'form',
                'target': 'current',
            }
        if self.sale_order_id:
            return {
                'type': 'ir.actions.act_window',
                'res_model': 'sale.order',
                'res_id': self.sale_order_id.id,
                'view_mode': 'form',
                'target': 'current',
            }
        return False

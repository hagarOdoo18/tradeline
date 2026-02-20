from odoo import tools, models, fields, api


class Commotion(models.Model):
    _name = "sales.commotion"
    _description = "Sales Commotion"
    _auto = False
    _rec_name = 'date'

    @api.depends('currency_id', 'date', 'price_total', 'price_average', 'residual')
    def _compute_amounts_in_user_currency(self):
        """Compute the amounts in the currency of the user."""
        user_currency_id = self.env.company.currency_id
        for record in self:
            date = record.date or fields.Date.today()
            company = record.company_id or self.env.company
            record.user_currency_price_total = record.currency_id._convert(
                record.price_total, user_currency_id, company, date,
            ) if record.currency_id else record.price_total
            record.user_currency_price_average = record.currency_id._convert(
                record.price_average, user_currency_id, company, date,
            ) if record.currency_id else record.price_average
            record.user_currency_residual = record.currency_id._convert(
                record.residual, user_currency_id, company, date,
            ) if record.currency_id else record.residual

    team_id = fields.Many2one('crm.team', string='Sales Team')
    name = fields.Char('Invoice #', readonly=True)
    date = fields.Date(readonly=True, string="Invoice Date")
    product_id = fields.Many2one('product.product', string='Product', readonly=True)
    product_qty = fields.Float(string='Product Quantity', readonly=True)
    uom_name = fields.Char(string='Reference Unit of Measure', readonly=True)
    payment_term_id = fields.Many2one(
        'account.payment.term', string='Payment Terms', readonly=True,
    )
    fiscal_position_id = fields.Many2one(
        'account.fiscal.position', string='Fiscal Position', readonly=True,
    )
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)
    categ_id = fields.Many2one(
        'product.category', string='Product Category', readonly=True,
    )
    journal_id = fields.Many2one('account.journal', string='Journal', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Partner', readonly=True)
    commercial_partner_id = fields.Many2one(
        'res.partner', string='Partner Company', help="Commercial Entity",
    )
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    user_id = fields.Many2one('res.users', string='Salesperson', readonly=True)
    branch_id = fields.Many2one('res.branch', string='Branch', readonly=True)
    price_total = fields.Float(string='Untaxed Total', readonly=True)
    user_currency_price_total = fields.Float(
        string="Total Without Tax in Currency",
        compute='_compute_amounts_in_user_currency', digits=0,
    )
    price_average = fields.Float(
        string='Average Price', readonly=True, group_operator="avg",
    )
    user_currency_price_average = fields.Float(
        string="Average Price in Currency",
        compute='_compute_amounts_in_user_currency', digits=0,
    )
    currency_rate = fields.Float(
        string='Currency Rate', readonly=True,
        group_operator="avg", groups="base.group_multi_currency",
    )
    nbr = fields.Integer(string='Line Count', readonly=True)
    move_id = fields.Many2one('account.move', string='Invoice', readonly=True)
    move_type = fields.Selection([
        ('out_invoice', 'Customer Invoice'),
        ('in_invoice', 'Vendor Bill'),
        ('out_refund', 'Customer Credit Note'),
        ('in_refund', 'Vendor Credit Note'),
    ], string='Type', readonly=True)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('posted', 'Posted'),
        ('cancel', 'Cancelled'),
    ], string='Invoice Status', readonly=True)
    date_due = fields.Date(string='Due Date', readonly=True)
    account_id = fields.Many2one(
        'account.account', string='Revenue/Expense Account',
        readonly=True,
    )
    partner_bank_id = fields.Many2one(
        'res.partner.bank', string='Bank Account', readonly=True,
    )
    residual = fields.Float(string='Due Amount', readonly=True)
    user_currency_residual = fields.Float(
        string="Total Residual",
        compute='_compute_amounts_in_user_currency', digits=0,
    )
    country_id = fields.Many2one(
        'res.country', string="Partner Company's Country",
    )
    amount_total = fields.Float(string='Total', readonly=True)

    _order = 'date desc'

    _depends = {
        'account.move': [
            'amount_total_signed', 'commercial_partner_id', 'company_id',
            'currency_id', 'invoice_date_due', 'invoice_date','branch_id',
            'fiscal_position_id', 'journal_id', 'name', 'partner_bank_id',
            'partner_id', 'invoice_payment_term_id', 'amount_residual_signed',
            'state', 'move_type', 'invoice_user_id',
        ],
        'account.move.line': [
            'account_id', 'move_id', 'price_subtotal', 'product_id',
            'quantity', 'product_uom_id',
        ],
        'product.product': ['product_tmpl_id'],
        'product.template': ['categ_id'],
        'uom.uom': ['category_id', 'factor', 'name', 'uom_type'],
        'res.currency.rate': ['currency_id', 'name'],
        'res.partner': ['country_id'],
    }

    def _select(self):
        return """
            SELECT sub.id, sub.name, sub.date, sub.product_id,
                sub.partner_id, sub.country_id, sub.branch_id,
                sub.payment_term_id, sub.uom_name, sub.currency_id,
                sub.journal_id, sub.fiscal_position_id, sub.user_id,
                sub.company_id, sub.nbr, sub.move_id, sub.move_type,
                sub.state, sub.categ_id, sub.date_due, sub.account_id,
                sub.partner_bank_id, sub.product_qty,
                sub.price_total AS price_total,
                sub.price_average AS price_average,
                sub.amount_total AS amount_total,
                sub.currency_rate AS currency_rate,
                sub.residual AS residual,
                sub.commercial_partner_id AS commercial_partner_id,
                sub.team_id AS team_id
        """

    def _sub_select(self):
        return """
                SELECT aml.id AS id,
                    am.invoice_date AS date,
                    am.name AS name,
                    aml.product_id,
                    am.partner_id,
                    am.invoice_payment_term_id AS payment_term_id,
                    u2.name AS uom_name,
                    am.currency_id,
                    am.journal_id,
                    am.fiscal_position_id,
                    am.invoice_user_id AS user_id,
                    am.branch_id AS branch_id,
                    am.company_id,
                    1 AS nbr,
                    am.id AS move_id,
                    am.move_type,
                    am.state,
                    pt.categ_id,
                    am.invoice_date_due AS date_due,
                    aml.account_id,
                    am.partner_bank_id,
                    SUM(
                        (move_type_sign.sign_qty * aml.quantity)
                        / COALESCE(u.factor, 1)
                        * COALESCE(u2.factor, 1)
                    ) AS product_qty,
                    (CASE WHEN pt.categ_id IN (53, 55, 51)
                        THEN SUM(aml.balance *-1 ) / 2
                        ELSE SUM(aml.balance *-1 )
                    END) AS price_total,
                    (CASE WHEN pt.categ_id IN (53, 55, 51)
                        THEN SUM(aml.price_total * move_type_sign.sign_qty) / 2
                        ELSE SUM(aml.price_total * move_type_sign.sign_qty)
                    END) AS amount_total,
                    SUM(ABS(aml.balance)) / CASE
                        WHEN SUM(
                            aml.quantity / COALESCE(u.factor, 1)
                            * COALESCE(u2.factor, 1)
                        ) <> 0::numeric
                        THEN SUM(
                            aml.quantity / COALESCE(u.factor, 1)
                            * COALESCE(u2.factor, 1)
                        )
                        ELSE 1::numeric
                    END AS price_average,
                    am.amount_residual_signed / GREATEST(
                        (SELECT count(*)
                         FROM account_move_line l
                         WHERE l.move_id = am.id
                           AND l.display_type = 'product'), 1
                    ) * count(*) * move_type_sign.sign AS residual,
                    am.commercial_partner_id AS commercial_partner_id,
                    COALESCE(partner.country_id, partner_am.country_id)
                        AS country_id,
                    am.team_id AS team_id,
                    CASE
                        WHEN am.currency_id != comp_curr.id
                        THEN (
                            SELECT COALESCE(r.rate, 1)
                            FROM res_currency_rate r
                            WHERE r.currency_id = am.currency_id
                              AND r.name <= COALESCE(am.invoice_date, NOW())
                              AND (r.company_id IS NULL
                                   OR r.company_id = am.company_id)
                            ORDER BY r.name DESC
                            LIMIT 1
                        )
                        ELSE 1.0
                    END AS currency_rate
        """

    def _from(self):
        return """
                FROM account_move_line aml
                JOIN account_move am ON am.id = aml.move_id
                JOIN res_partner partner
                    ON am.commercial_partner_id = partner.id
                JOIN res_partner partner_am ON am.partner_id = partner_am.id
                JOIN res_company comp ON comp.id = am.company_id
                JOIN res_currency comp_curr ON comp_curr.id = comp.currency_id
                LEFT JOIN product_product pr ON pr.id = aml.product_id
                LEFT JOIN product_template pt ON pt.id = pr.product_tmpl_id
                LEFT JOIN uom_uom u ON u.id = aml.product_uom_id
                LEFT JOIN uom_uom u2 ON u2.id = pt.uom_id
                JOIN (
                    SELECT id,
                        (CASE
                            WHEN am.move_type IN ('out_refund')
                            THEN -1 ELSE 1
                        END) AS sign,
                        (CASE
                            WHEN am.move_type IN ('out_refund')
                            THEN -1 ELSE 1
                        END) AS sign_qty
                    FROM account_move am
                ) AS move_type_sign ON move_type_sign.id = am.id
        """

    def _where(self):
        return """
                WHERE am.move_type IN (
                    'out_invoice', 'out_refund'
                )
                AND am.state = 'posted'
                AND aml.display_type = 'product'
                AND aml.account_id IS NOT NULL
        """

    def _group_by(self):
        return """
                GROUP BY aml.id, aml.product_id, am.invoice_date,
                    am.id, am.partner_id, am.invoice_payment_term_id,
                    u2.name, u2.id, am.currency_id, am.journal_id,
                    am.fiscal_position_id, am.invoice_user_id,
                    am.company_id, am.move_type, move_type_sign.sign,
                    am.state, pt.categ_id, am.invoice_date_due,
                    aml.account_id, am.partner_bank_id,
                    am.amount_residual_signed,
                    am.amount_total_signed,
                    am.commercial_partner_id,
                    COALESCE(partner.country_id, partner_am.country_id),
                    am.team_id, comp_curr.id,am.branch_id
        """

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute("""
            CREATE OR REPLACE VIEW %s AS (
                %s
                FROM (
                    %s %s %s %s
                ) AS sub
            )
        """ % (
            self._table,
            self._select(),
            self._sub_select(),
            self._from(),
            self._where(),
            self._group_by(),
        ))


class ReportInvoiceWithPayment(models.AbstractModel):
    _name = 'report.account.report_invoice_with_payments'
    _description = 'Account report with payment lines'

    @api.model
    def _get_report_values(self, docids, data=None):
        return {
            'doc_ids': docids,
            'doc_model': 'account.move',
            'docs': self.env['account.move'].browse(docids),
            'report_type': data.get('report_type') if data else '',
        }

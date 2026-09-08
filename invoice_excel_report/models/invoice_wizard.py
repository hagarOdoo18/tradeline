# -*- coding: utf-8 -*-
import base64
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR
from io import BytesIO

import xlsxwriter
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class AccountInvoiceWizard(models.TransientModel):
    _name = 'account.invoice.duo.wizard'
    _description = 'Account Invoice Wizard'

    partner_id = fields.Many2one('res.partner', string='Customer')
    branch_id = fields.Many2one('res.branch', string='Branch')
    number = fields.Char(string='Invoice Number')
    journal_id = fields.Many2many('account.journal', string='Journal')
    date_from = fields.Date(string='Date From')
    date_to = fields.Date(string='Date To')
    excel_file = fields.Binary(readonly=True)
    file_name = fields.Char(readonly=True)
    report_line_ids = fields.One2many(
        'account.invoice.duo.report.line', 'wizard_id', string='Report Lines', readonly=True,
    )
    report_summary = fields.Text(readonly=True)

    def _s(self, value):
        if isinstance(value, dict):
            lang = self.env.lang or 'en_US'
            return str(value.get(lang) or next(iter(value.values()), '') or '')
        return str(value or '')

    @staticmethod
    def _signed_settlement_amount(reconciliation_sign, amount):
        """Use the debit/credit side of the reconciliation itself."""
        return abs(float(amount or 0)) * int(reconciliation_sign)

    @staticmethod
    def _settlement_label(counterpart_type, counterpart_name, journal_name, is_payment):
        if is_payment:
            return journal_name or 'Payment'
        if counterpart_type == 'out_refund':
            return 'Credit Note Offset (non-cash) — %s' % (counterpart_name or '')
        if counterpart_type == 'out_invoice':
            return 'Invoice Offset (non-cash) — %s' % (counterpart_name or '')
        return 'Journal Settlement (non-cash) — %s' % (journal_name or counterpart_name or 'Journal Entry')

    @staticmethod
    def _combine_settlements(reconciled, pos):
        """Prefer tender splits when their total equals the reconciled allocation.

        When totals differ, reconciliation is the accounting evidence for the
        amount settled. Returning its rows avoids inventing a balancing plug.
        """
        combined = {}
        for invoice_id in set(reconciled) | set(pos):
            pos_rows = pos.get(invoice_id, [])
            rec_rows = reconciled.get(invoice_id, [])
            if pos_rows and rec_rows:
                pos_total = sum(row[1] for row in pos_rows)
                rec_total = sum(row[1] for row in rec_rows)
                has_noncash_offset = any(row[2] == 'offset' for row in rec_rows)
                combined[invoice_id] = (
                    pos_rows if abs(pos_total - rec_total) < 1e-9 and not has_noncash_offset else rec_rows
                )
            else:
                combined[invoice_id] = pos_rows or rec_rows
        return combined

    def action_account_invoice_search(self):
        invoices = self._search_invoices()
        if not invoices:
            raise UserError(_('No invoices found for the selected criteria.'))
        return self.generate_excel(invoices)

    def action_view_report(self):
        self.ensure_one()
        invoices = self._search_invoices()
        if not invoices:
            raise UserError(_('No invoices found for the selected criteria.'))
        report_rows = self._prepare_report_rows(invoices)
        self.write({
            'report_line_ids': [(5, 0, 0)] + [
                (0, 0, self._report_line_values(row)) for row in report_rows
            ],
            'report_summary': self._report_currency_summary(report_rows),
        })
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def _search_invoices(self):
        domain = [('move_type', 'in', ('out_invoice', 'out_refund'))]
        for field_name, operator, value in (
            ('invoice_date', '>=', self.date_from), ('invoice_date', '<=', self.date_to),
            ('partner_id', '=', self.partner_id.id if self.partner_id else False),
            ('name', '=', self.number),
            ('branch_id', '=', self.branch_id.id if self.branch_id else False),
        ):
            if value:
                domain.append((field_name, operator, value))
        invoices = self.env['account.move'].search(domain)
        if self.journal_id and invoices:
            rows, _basis = self._prepare_report_settlements(invoices)
            wanted = set(self.journal_id.ids)
            invoices = invoices.filtered(
                lambda move: any(row[3] in wanted for row in rows.get(move.id, []))
            )
        return invoices

    @api.model
    def get_invoices_by_journal(self, journal_id, date_from=None, date_to=None):
        jids = tuple(journal_id.ids)
        if not jids:
            return self.env['account.move']
        params = {'jids': list(jids), 'df': date_from, 'dt': date_to}
        self.env.cr.execute("""
            WITH rec AS (
                SELECT debit_move_id invoice_line_id, credit_move_id counterpart_line_id FROM account_partial_reconcile
                UNION ALL
                SELECT credit_move_id, debit_move_id FROM account_partial_reconcile
            )
            SELECT DISTINCT invoice_line.move_id
              FROM rec
              JOIN account_move_line invoice_line ON invoice_line.id = rec.invoice_line_id
              JOIN account_account account ON account.id = invoice_line.account_id
                   AND account.account_type = 'asset_receivable'
              JOIN account_move invoice ON invoice.id = invoice_line.move_id
                   AND invoice.move_type IN ('out_invoice', 'out_refund') AND invoice.state = 'posted'
              JOIN account_move_line payment_line ON payment_line.id = rec.counterpart_line_id
              JOIN account_payment payment ON payment.move_id = payment_line.move_id
             WHERE payment.journal_id = ANY(%(jids)s)
               AND (%(df)s IS NULL OR payment.date >= %(df)s::date)
               AND (%(dt)s IS NULL OR payment.date <= %(dt)s::date)
        """, params)
        invoice_ids = {row[0] for row in self.env.cr.fetchall()}
        if 'pos.payment' in self.env:
            self.env.cr.execute("""
                SELECT DISTINCT move.id
                  FROM pos_payment payment
                  JOIN pos_payment_method method ON method.id = payment.payment_method_id
                  JOIN pos_order pos_order ON pos_order.id = payment.pos_order_id
                  JOIN account_move move ON move.id = pos_order.account_move
                 WHERE method.journal_id = ANY(%(jids)s)
                   AND (%(df)s IS NULL OR payment.payment_date::date >= %(df)s::date)
                   AND (%(dt)s IS NULL OR payment.payment_date::date <= %(dt)s::date)
            """, params)
            invoice_ids.update(row[0] for row in self.env.cr.fetchall())
        return self.env['account.move'].browse(list(invoice_ids))

    def _reconciled_settlements(self, invoice_ids):
        """Return {invoice_id: [(label, signed invoice-currency amount, kind)]}."""
        if not invoice_ids:
            return {}
        pos_joins = pos_columns = ''
        if 'pos.payment' in self.env and 'account_move_id' in self.env['pos.payment']._fields:
            pos_joins = """
                LEFT JOIN LATERAL (
                    SELECT min(pp.id) id,
                           CASE WHEN count(DISTINCT pm.journal_id) = 1
                                THEN min(pm.journal_id) END journal_id,
                           count(DISTINCT pm.journal_id) method_count
                      FROM pos_payment pp
                      JOIN pos_payment_method pm ON pm.id = pp.payment_method_id
                     WHERE pp.account_move_id = counterpart.id
                ) pos_payment ON TRUE
                LEFT JOIN account_journal pos_journal ON pos_journal.id = pos_payment.journal_id
            """
            pos_columns = ', pos_payment.id, pos_journal.name, pos_payment.method_count, pos_payment.journal_id'
        else:
            pos_columns = ', NULL, NULL, NULL, NULL'
        self.env.cr.execute("""
            WITH rec AS (
                SELECT debit_move_id invoice_line_id, credit_move_id counterpart_line_id,
                       debit_amount_currency invoice_currency_amount, 1 reconciliation_sign
                  FROM account_partial_reconcile
                UNION ALL
                SELECT credit_move_id, debit_move_id, credit_amount_currency, -1
                  FROM account_partial_reconcile
            )
            SELECT invoice_line.move_id, rec.reconciliation_sign, counterpart.move_type,
                   counterpart.name, journal.name, payment.id, journal.id,
                   rec.invoice_currency_amount %s
              FROM rec
              JOIN account_move_line invoice_line ON invoice_line.id = rec.invoice_line_id
              JOIN account_account account ON account.id = invoice_line.account_id
                   AND account.account_type = 'asset_receivable'
              JOIN account_move invoice ON invoice.id = invoice_line.move_id
              JOIN account_move_line counterpart_line ON counterpart_line.id = rec.counterpart_line_id
              JOIN account_move counterpart ON counterpart.id = counterpart_line.move_id
              JOIN account_journal journal ON journal.id = counterpart.journal_id
              LEFT JOIN account_payment payment ON payment.move_id = counterpart.id
              %s
             WHERE invoice_line.move_id = ANY(%%s)
        """ % (pos_columns, pos_joins), (invoice_ids,))
        result = {}
        for row in self.env.cr.fetchall():
            (invoice_id, reconciliation_sign, counterpart_type, counterpart_name,
             journal_name, payment_id, journal_id, currency_amount, pos_payment_id, pos_journal_name,
             pos_method_count, pos_journal_id) = row
            # Zero is meaningful in foreign currency exchange reconciliations.
            # Do not substitute apr.amount (company currency).
            amount = self._signed_settlement_amount(reconciliation_sign, currency_amount)
            if not amount:
                continue
            if pos_payment_id:
                label = ('POS Payment (multiple methods)' if pos_method_count > 1
                         else self._s(pos_journal_name) or 'POS Payment')
                kind = 'pos_reconciliation'
                journal_id = pos_journal_id
            else:
                label = self._settlement_label(
                    counterpart_type, counterpart_name, self._s(journal_name), bool(payment_id)
                )
                kind = 'payment' if payment_id else 'offset'
            result.setdefault(invoice_id, []).append((label, amount, kind, journal_id))
        return result

    def _pos_settlements(self, invoice_ids):
        if not invoice_ids or 'pos.payment' not in self.env:
            return {}
        self.env.cr.execute("""
            SELECT move.id, journal.name, payment.amount, method.journal_id
              FROM account_move move
              JOIN pos_order pos_order ON pos_order.account_move = move.id
              JOIN pos_payment payment ON payment.pos_order_id = pos_order.id
              JOIN pos_payment_method method ON method.id = payment.payment_method_id
              LEFT JOIN account_journal journal ON journal.id = method.journal_id
             WHERE move.id = ANY(%s)
             ORDER BY payment.id
        """, (invoice_ids,))
        result = {}
        for invoice_id, journal_name, amount, journal_id in self.env.cr.fetchall():
            # POS owns the sign: split refunds can contain negative and positive tenders.
            result.setdefault(invoice_id, []).append(
                (self._s(journal_name) or 'POS Payment', float(amount or 0), 'pos', journal_id)
            )
        return result

    @staticmethod
    def _allocate_credit(method_rows, original_total, prior_credit, credit_total,
                         remaining=None, currency=None):
        """Allocate credit across net-positive methods and remaining capacity."""
        original_total = abs(float(original_total or 0))
        credit_total = abs(float(credit_total or 0))
        if not original_total or not method_rows or not credit_total:
            return []
        start = min(abs(float(prior_credit or 0)), original_total)
        end = min(start + credit_total, original_total)
        allocatable_principal = max(end - start, 0)
        grouped = {}
        for label, amount, _kind, journal_id in method_rows:
            key = (journal_id, label)
            grouped[key] = grouped.get(key, 0.0) + float(amount or 0)
        if any(amount < 0 for amount in grouped.values()):
            return []
        positive = {key: amount for key, amount in grouped.items() if amount > 0}
        paid_total = sum(positive.values())
        evidenced_fraction = min(paid_total / original_total, 1.0)
        target = allocatable_principal * evidenced_fraction
        if not target or not paid_total:
            return []
        remaining = remaining if remaining is not None else dict(positive)
        if currency:
            for key in list(remaining):
                remaining[key] = currency.round(remaining[key])
        target = min(target, sum(max(remaining.get(key, 0.0), 0.0) for key in positive))
        active = {key for key in positive if remaining.get(key, 0.0) > 0}
        raw_allocations = {key: 0.0 for key in positive}
        left = target
        while active and left > 1e-12:
            weight_total = sum(positive[key] for key in active)
            capped = []
            for key in active:
                share = left * positive[key] / weight_total
                capacity = max(remaining.get(key, 0.0) - raw_allocations[key], 0.0)
                if share >= capacity - 1e-12:
                    raw_allocations[key] += capacity
                    left -= capacity
                    capped.append(key)
            if not capped:
                for key in active:
                    raw_allocations[key] += left * positive[key] / weight_total
                left = 0.0
            else:
                active.difference_update(capped)

        items = sorted(positive, key=lambda key: (key[0] or 0, key[1] or ''))
        if currency:
            quantum = Decimal(str(currency.rounding))
            target_units = int(Decimal(str(currency.round(target))) / quantum)
            unit_rows = []
            for key in items:
                exact_units = Decimal(str(raw_allocations[key])) / quantum
                capacity_units = int(
                    (Decimal(str(max(currency.round(remaining.get(key, 0.0)), 0.0))) / quantum)
                    .to_integral_value(rounding=ROUND_FLOOR)
                )
                floor_units = min(
                    int(exact_units.to_integral_value(rounding=ROUND_FLOOR)), capacity_units
                )
                unit_rows.append([key, floor_units, exact_units - floor_units, capacity_units])
            units_left = target_units - sum(row[1] for row in unit_rows)
            for row in sorted(unit_rows, key=lambda item: (-item[2], item[0][0] or 0, item[0][1] or '')):
                if units_left <= 0:
                    break
                if row[1] < row[3]:
                    row[1] += 1
                    units_left -= 1
            rounded_allocations = {row[0]: float(quantum * row[1]) for row in unit_rows}
        else:
            rounded_allocations = raw_allocations

        allocations = []
        for journal_id, label in items:
            amount = rounded_allocations[(journal_id, label)]
            if amount > 0:
                allocations.append((label, -amount, 'attributed', journal_id))
                new_remaining = remaining.get((journal_id, label), 0.0) - amount
                remaining[(journal_id, label)] = (
                    currency.round(new_remaining) if currency else new_remaining
                )
        return allocations

    def _prepare_report_settlements(self, invoices):
        """Return displayed method rows and their report-only allocation basis."""
        refunds = invoices.filtered(lambda move: move.move_type == 'out_refund')
        originals = refunds.mapped('reversed_entry_id').filtered(
            lambda move: move.move_type == 'out_invoice'
        )
        siblings = self.env['account.move'].search([
            ('reversed_entry_id', 'in', originals.ids),
            ('move_type', '=', 'out_refund'), ('state', '=', 'posted'),
        ]) if originals else self.env['account.move']
        all_moves = invoices | originals | siblings
        reconciled = self._reconciled_settlements(all_moves.ids)
        financial_reconciled = {
            move_id: [row for row in rows if row[2] in ('payment', 'pos_reconciliation')]
            for move_id, rows in reconciled.items()
        }
        actual = self._combine_settlements(
            financial_reconciled, self._pos_settlements(all_moves.ids)
        )
        result = {move.id: list(actual.get(move.id, [])) for move in invoices}
        basis = {
            move.id: ('Recorded payment' if result.get(move.id) else 'No recorded payment')
            for move in invoices
        }
        for credit in refunds.filtered(lambda move: move.state != 'posted'):
            result[credit.id] = list(actual.get(credit.id, []))
            basis[credit.id] = 'Not posted; attribution not applied'

        siblings_by_original = {}
        for credit in siblings.sorted(key=lambda move: (str(move.invoice_date or ''), move.id)):
            siblings_by_original.setdefault(credit.reversed_entry_id.id, []).append(credit)
        for original in originals:
            method_rows = actual.get(original.id, [])
            source_totals = {}
            for label, amount, _kind, journal_id in method_rows:
                key = (journal_id, label)
                source_totals[key] = source_totals.get(key, 0.0) + float(amount or 0)
            has_negative_source = any(amount < 0 for amount in source_totals.values())
            remaining = {key: amount for key, amount in source_totals.items() if amount > 0}
            prior_credit = 0.0
            for credit in siblings_by_original.get(original.id, []):
                credit_total = abs(credit.amount_total_in_currency_signed)
                actual_credit_rows = actual.get(credit.id, [])
                attributed = []
                attribution_reason = ''
                if actual_credit_rows:
                    refund_totals = {}
                    for label, amount, _kind, journal_id in actual_credit_rows:
                        key = (journal_id, label)
                        refund_totals[key] = refund_totals.get(key, 0.0) + float(amount or 0)
                    inconsistent_refund = False
                    for key, signed_amount in refund_totals.items():
                        consumed = abs(signed_amount)
                        if key not in remaining or consumed > remaining[key] + 1e-9:
                            inconsistent_refund = True
                            break
                        remaining[key] = credit.currency_id.round(remaining[key] - consumed)
                    if inconsistent_refund:
                        remaining.clear()
                    if credit.id in result:
                        basis[credit.id] = 'Recorded refund payment; no attribution'
                elif (method_rows and not has_negative_source and original.state == 'posted'
                          and original.company_id == credit.company_id
                          and original.currency_id == credit.currency_id):
                    attributed = self._allocate_credit(
                        method_rows, original.amount_total_in_currency_signed,
                        prior_credit, credit_total, remaining, credit.currency_id,
                    )
                    attribution_reason = (
                        'Attributed to original payment (report only)' if attributed
                        else 'No remaining original payment capacity; no attribution'
                    )
                elif has_negative_source:
                    attribution_reason = 'Unsupported negative original payment mix; no attribution'
                elif original.currency_id != credit.currency_id:
                    attribution_reason = 'Original invoice currency mismatch; no attribution'
                elif original.company_id != credit.company_id:
                    attribution_reason = 'Original invoice company mismatch; no attribution'
                elif original.state != 'posted':
                    attribution_reason = 'Original invoice not posted; no attribution'
                else:
                    attribution_reason = 'No original payment evidence; no attribution'
                if credit.id in result and not actual_credit_rows:
                    result[credit.id] = attributed
                    basis[credit.id] = attribution_reason
                # Every posted sibling consumes original credit principal, including
                # siblings with actual refunds, so later credits cannot reuse capacity.
                prior_credit += credit_total
        for credit in refunds.filtered(lambda move: not move.reversed_entry_id):
            if not actual.get(credit.id):
                basis[credit.id] = 'No original invoice link; no attribution'
        return result, basis

    @staticmethod
    def _invoice_tax_values(invoice, sign):
        currency_round = invoice.currency_id.round
        tax_14 = currency_round((invoice.tax_t1 or 0.0) * sign)
        return {
            'tax_14': tax_14,
            'subtotal_with_tax_14': currency_round(
                invoice.amount_untaxed_in_currency_signed + tax_14
            ),
            'tax_1': currency_round((invoice.tax_t2 or 0.0) * sign),
            'tax_2': currency_round((invoice.tax_t2_t or 0.0) * sign),
            'tax_3': currency_round((invoice.tax_t3 or 0.0) * sign),
            'tax_5': currency_round((invoice.tax_t5 or 0.0) * sign),
        }

    def _prepare_report_rows(self, invoices):
        """Build the rows used by both the on-screen report and Excel."""
        settlements, allocation_basis = self._prepare_report_settlements(invoices)
        report_rows = []
        for idx, invoice in enumerate(invoices, start=1):
            rows = settlements.get(invoice.id) or [('', 0, '', False)]
            rounded_rows = [
                (label, invoice.currency_id.round(amount), kind, journal_id)
                for label, amount, kind, journal_id in rows
            ]
            total_net = invoice.currency_id.round(invoice.amount_total_in_currency_signed)
            report_due = invoice.currency_id.round(
                total_net - sum(item[1] for item in rounded_rows)
            )
            sign = 1 if invoice.move_type == 'out_invoice' else -1
            tax_values = self._invoice_tax_values(invoice, sign)
            for offset, (label, amount, kind, journal_id) in enumerate(rounded_rows):
                show_invoice = offset == 0
                report_rows.append({
                    'sequence': idx,
                    'invoice': invoice,
                    'invoice_date': invoice.invoice_date,
                    'invoice_name': self._s(invoice.name),
                    'branch_name': self._s(invoice.branch_id.name) or 'None',
                    'customer_name': self._s(invoice.partner_id.name) or 'None',
                    'phone': self._s(invoice.partner_id.phone) or 'None',
                    'payment_label': self._s(label) or 'None',
                    'payment_amount': amount,
                    'payment_kind': kind,
                    'payment_journal_id': journal_id,
                    'reference': self._s(invoice.invoice_origin or invoice.ref),
                    'tax_excluded': (
                        invoice.amount_untaxed_in_currency_signed if show_invoice else 0
                    ),
                    'tax_14': tax_values['tax_14'] if show_invoice else 0,
                    'subtotal_with_tax_14': (
                        tax_values['subtotal_with_tax_14'] if show_invoice else 0
                    ),
                    'tax_1': tax_values['tax_1'] if show_invoice else 0,
                    'tax_2': tax_values['tax_2'] if show_invoice else 0,
                    'tax_3': tax_values['tax_3'] if show_invoice else 0,
                    'tax_5': tax_values['tax_5'] if show_invoice else 0,
                    'total_net': total_net if show_invoice else 0,
                    'report_amount_due': report_due if show_invoice else 0,
                    'accounting_amount_due': (
                        invoice.currency_id.round(invoice.amount_residual * sign)
                        if show_invoice else 0
                    ),
                    'currency': invoice.currency_id,
                    'allocation_basis': allocation_basis.get(invoice.id, ''),
                    'show_invoice': show_invoice,
                })
        return report_rows

    @staticmethod
    def _report_line_values(row):
        return {
            'sequence': row['sequence'],
            'invoice_id': row['invoice'].id,
            'invoice_date': row['invoice_date'],
            'branch_name': row['branch_name'],
            'customer_name': row['customer_name'],
            'phone': row['phone'],
            'payment_label': row['payment_label'],
            'payment_amount': row['payment_amount'],
            'reference': row['reference'],
            'tax_excluded': row['tax_excluded'],
            'tax_14': row['tax_14'],
            'subtotal_with_tax_14': row['subtotal_with_tax_14'],
            'tax_1': row['tax_1'],
            'tax_2': row['tax_2'],
            'tax_3': row['tax_3'],
            'tax_5': row['tax_5'],
            'total_net': row['total_net'],
            'report_amount_due': row['report_amount_due'],
            'accounting_amount_due': row['accounting_amount_due'],
            'currency_id': row['currency'].id,
            'allocation_basis': row['allocation_basis'],
            'is_attributed': row['payment_kind'] == 'attributed',
        }

    def _report_currency_summary(self, report_rows):
        totals = {}
        for row in report_rows:
            currency = row['currency']
            values = totals.setdefault(currency.id, {
                'currency': currency,
                'total_net': 0.0,
                'payment_amount': 0.0,
                'report_amount_due': 0.0,
                'accounting_amount_due': 0.0,
            })
            values['payment_amount'] += row['payment_amount']
            if row['show_invoice']:
                values['total_net'] += row['total_net']
                values['report_amount_due'] += row['report_amount_due']
                values['accounting_amount_due'] += row['accounting_amount_due']
        lines = []
        for currency_id in sorted(
                totals, key=lambda item: (totals[item]['currency'].name or '', item)):
            values = totals[currency_id]
            currency = values['currency']
            lines.append(_(
                '%(currency)s — Total Net: %(total)s | Payment Amount: %(payment)s | '
                'Report Amount Due: %(report_due)s | Accounting Amount Due: %(accounting_due)s',
                currency=self._s(currency.name),
                total=currency.round(values['total_net']),
                payment=currency.round(values['payment_amount']),
                report_due=currency.round(values['report_amount_due']),
                accounting_due=currency.round(values['accounting_amount_due']),
            ))
        return '\n'.join(lines)

    def generate_excel(self, invoices):
        output = BytesIO()
        workbook = xlsxwriter.Workbook(output, {'in_memory': True})
        sheet = workbook.add_worksheet('Account Invoices Report')
        header_format = workbook.add_format({'bold': True, 'border': 1, 'bg_color': '#AAB7B8',
            'align': 'center', 'valign': 'vcenter', 'text_wrap': True, 'font_size': 10})
        cell_format = workbook.add_format({'font_name': 'KacstBook', 'font_size': 10,
            'align': 'center', 'valign': 'vcenter', 'text_wrap': True, 'border': 1})
        amount_formats = {}
        headers = ['No', 'Date', 'Invoice Number', 'Branch', 'Customer Name', 'Phone',
            'Payment', 'Payment Amount', 'Ref', 'Tax Excluded', 'Tax14', 'Total',
            'Tax1', 'Tax2', 'Tax3', 'Tax5', 'Total Net', 'Amount Due', 'Currency',
            'Accounting Amount Due', 'Allocation Basis']
        sheet.set_column(1, 50, 20)
        sheet.set_column(6, 6, 48)
        sheet.freeze_panes(1, 0)
        for col, header in enumerate(headers):
            sheet.write(0, col, header, header_format)

        def write_row(row, report_row):
            inv = report_row['invoice']
            currency_round = inv.currency_id.round
            values = [report_row['sequence'], str(report_row['invoice_date'] or ''),
                report_row['invoice_name'], report_row['branch_name'], report_row['customer_name'],
                report_row['phone'], report_row['payment_label'],
                currency_round(report_row['payment_amount']), report_row['reference'],
                report_row['tax_excluded'], report_row['tax_14'],
                report_row['subtotal_with_tax_14'], report_row['tax_1'], report_row['tax_2'],
                report_row['tax_3'], report_row['tax_5'], report_row['total_net'],
                report_row['report_amount_due'],
                self._s(inv.currency_id.name),
                report_row['accounting_amount_due'], report_row['allocation_basis']]
            for col, value in enumerate(values):
                fmt = cell_format
                if col == 7 or 9 <= col <= 17 or col == 19:
                    decimals = inv.currency_id.decimal_places
                    if decimals not in amount_formats:
                        amount_formats[decimals] = workbook.add_format({
                            'font_name': 'KacstBook', 'font_size': 10,
                            'align': 'right', 'valign': 'vcenter', 'border': 1,
                            'num_format': '#,##0' + ('.' + '0' * decimals if decimals else ''),
                        })
                    fmt = amount_formats[decimals]
                sheet.write(row, col, value, fmt)

        row = 1
        for report_row in self._prepare_report_rows(invoices):
            write_row(row, report_row)
            sheet.set_row(row, 30)
            row += 1
        row += 1
        sheet.merge_range(row, 0, row, len(headers) - 1,
            'Payment Amount contains recorded methods or report-only attribution stated in Allocation Basis. '
            'Amount Due is the report balance; Accounting Amount Due is Odoo residual. No accounting entries are changed.',
            cell_format)
        workbook.close()
        output.seek(0)
        self.file_name = 'invoices_%s.xlsx' % datetime.today().date()
        self.excel_file = base64.b64encode(output.read())
        return {'type': 'ir.actions.act_window', 'res_model': self._name,
                'res_id': self.id, 'view_mode': 'form', 'target': 'new'}


class AccountInvoiceReportLine(models.TransientModel):
    _name = 'account.invoice.duo.report.line'
    _description = 'Account Invoice Report Line'
    _order = 'sequence, id'

    wizard_id = fields.Many2one(
        'account.invoice.duo.wizard', required=True, ondelete='cascade', index=True,
    )
    sequence = fields.Integer(string='No', readonly=True)
    invoice_id = fields.Many2one('account.move', string='Invoice Number', readonly=True)
    invoice_date = fields.Date(string='Date', readonly=True)
    branch_name = fields.Char(string='Branch', readonly=True)
    customer_name = fields.Char(string='Customer Name', readonly=True)
    phone = fields.Char(readonly=True)
    payment_label = fields.Char(string='Payment', readonly=True)
    payment_amount = fields.Monetary(readonly=True, currency_field='currency_id')
    reference = fields.Char(string='Ref', readonly=True)
    tax_excluded = fields.Monetary(readonly=True, currency_field='currency_id')
    tax_14 = fields.Monetary(string='Tax14', readonly=True, currency_field='currency_id')
    subtotal_with_tax_14 = fields.Monetary(
        string='Total', readonly=True, currency_field='currency_id',
    )
    tax_1 = fields.Monetary(string='Tax1', readonly=True, currency_field='currency_id')
    tax_2 = fields.Monetary(string='Tax2', readonly=True, currency_field='currency_id')
    tax_3 = fields.Monetary(string='Tax3', readonly=True, currency_field='currency_id')
    tax_5 = fields.Monetary(string='Tax5', readonly=True, currency_field='currency_id')
    total_net = fields.Monetary(readonly=True, currency_field='currency_id')
    report_amount_due = fields.Monetary(
        string='Amount Due', readonly=True, currency_field='currency_id',
    )
    accounting_amount_due = fields.Monetary(
        readonly=True, currency_field='currency_id',
    )
    currency_id = fields.Many2one('res.currency', readonly=True)
    allocation_basis = fields.Char(readonly=True)
    is_attributed = fields.Boolean(readonly=True)

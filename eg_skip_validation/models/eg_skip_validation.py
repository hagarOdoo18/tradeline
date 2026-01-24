from odoo import api, models, _

from datetime import date

class AccountEdiFormat(models.Model):
    _inherit = 'account.edi.format'

    def _check_move_configuration(self, invoice):
        errors = []
        if self.code != 'eg_eta':
            return errors
        if invoice.journal_id.l10n_eg_skip_eta_validation :
            return errors

        if invoice.journal_id.l10n_eg_branch_id.vat == invoice.partner_id.vat:
            errors.append(_("You cannot issue an invoice to a partner with the same VAT number as the branch."))
        if not self._l10n_eg_get_eta_token_domain(invoice.company_id.l10n_eg_production_env):
            errors.append(_("Please configure the token domain from the system parameters"))
        if not self._l10n_eg_get_eta_api_domain(invoice.company_id.l10n_eg_production_env):
            errors.append(_("Please configure the API domain from the system parameters"))
        if not all([invoice.journal_id.l10n_eg_branch_id, invoice.journal_id.l10n_eg_branch_identifier,
                    invoice.journal_id.l10n_eg_activity_type_id]):
            errors.append(_("Please set the all the ETA information on the invoice's journal"))
        if not self._l10n_eg_validate_info_address(invoice.journal_id.l10n_eg_branch_id):
            errors.append(_("Please add all the required fields in the branch details"))
        if not self._l10n_eg_validate_info_address(invoice.partner_id, invoice=invoice):
            errors.append(_("Please add all the required fields in the customer details"))
        if not all(aml.product_uom_id.l10n_eg_unit_code_id.code for aml in
                   invoice.invoice_line_ids.filtered(lambda x: x.display_type not in ('line_note', 'line_section'))):
            errors.append(_("Please make sure the invoice lines UoM codes are all set up correctly"))
        if not all(tax.l10n_eg_eta_code for tax in invoice.invoice_line_ids.filtered(
                lambda x: x.display_type not in ('line_note', 'line_section')).tax_ids):
            errors.append(_("Please make sure the invoice lines taxes all have the correct ETA tax code"))
        if not all(aml.product_id.l10n_eg_eta_code or aml.product_id.barcode for aml in
                   invoice.invoice_line_ids.filtered(lambda x: x.display_type not in ('line_note', 'line_section'))):
            errors.append(_("Please make sure the EGS/GS1 Barcode is set correctly on all products"))
        return errors

    @api.model
    def _l10n_eg_eta_prepare_eta_invoice(self, invoice):
        AccountTax = self.env['account.tax']
        base_amls = invoice.line_ids.filtered(lambda x: x.display_type == 'product')
        base_lines = [invoice._prepare_product_base_line_for_taxes_computation(x) for x in base_amls]
        tax_amls = invoice.line_ids.filtered('tax_repartition_line_id')
        tax_lines = [invoice._prepare_tax_line_for_taxes_computation(x) for x in tax_amls]
        AccountTax._add_tax_details_in_base_lines(base_lines, invoice.company_id)
        AccountTax._round_base_lines_tax_details(base_lines, invoice.company_id, tax_lines=tax_lines)

        # Tax amounts per line.

        def grouping_function_base_line(base_line, tax_data):
            if not tax_data:
                return None
            tax = tax_data['tax']
            code_split = tax.l10n_eg_eta_code.split('_')
            return {
                'rate': abs(tax.amount) if tax.amount_type != 'fixed' else 0,
                'tax_type': code_split[0].upper(),
                'sub_type': code_split[1].upper(),
            }

        base_lines_aggregated_values = AccountTax._aggregate_base_lines_tax_details(base_lines,
                                                                                    grouping_function_base_line)
        invoice_line_data, totals = self._l10n_eg_eta_prepare_invoice_lines_data(invoice, base_lines_aggregated_values)

        # Tax amounts for the whole document.

        def grouping_function_global(base_line, tax_data):
            if not tax_data:
                return None
            tax = tax_data['tax']
            code_split = tax.l10n_eg_eta_code.split('_')
            return {
                'tax_type': code_split[0].upper(),
            }

        def grouping_function_total_amount(base_line, tax_data):
            return True if tax_data else None

        base_lines_aggregated_values_total_amount = AccountTax._aggregate_base_lines_tax_details(base_lines,
                                                                                                 grouping_function_total_amount)
        values_per_grouping_key_total_amount = AccountTax._aggregate_base_lines_aggregated_values(
            base_lines_aggregated_values_total_amount)

        base_lines_aggregated_values = AccountTax._aggregate_base_lines_tax_details(base_lines,
                                                                                    grouping_function_global)
        values_per_grouping_key = AccountTax._aggregate_base_lines_aggregated_values(base_lines_aggregated_values)

        date_string = invoice.invoice_date.strftime('%Y-%m-%dT%H:%M:%SZ')
        date_string = date.today().strftime('%Y-%m-%dT%H:%M:%SZ')
        eta_invoice = {
            'issuer': self._l10n_eg_eta_prepare_address_data(invoice.journal_id.l10n_eg_branch_id, invoice,
                                                             issuer=True, ),
            'receiver': self._l10n_eg_eta_prepare_address_data(invoice.partner_id, invoice),
            'documentType': 'i' if invoice.move_type == 'out_invoice' else 'c' if invoice.move_type == 'out_refund' else 'd' if invoice.move_type == 'in_refund' else '',
            'documentTypeVersion': '1.0',
            'dateTimeIssued': date_string,
            'taxpayerActivityCode': invoice.journal_id.l10n_eg_activity_type_id.code,
            'internalID': invoice.name,
        }
        eta_invoice.update({
            'invoiceLines': invoice_line_data,
            'taxTotals': [
                {
                    'taxType': grouping_key['tax_type'],
                    'amount': self._l10n_eg_edi_round(abs(tax_values['tax_amount'])),
                }
                for grouping_key, tax_values in values_per_grouping_key.items()
                if grouping_key
            ],
            'totalDiscountAmount': self._l10n_eg_edi_round(totals['discount_total']),
            'totalSalesAmount': self._l10n_eg_edi_round(totals['total_price_subtotal_before_discount']),
            'netAmount': self._l10n_eg_edi_round(
                sum(x['base_amount'] for x in values_per_grouping_key_total_amount.values())),
            'totalAmount': self._l10n_eg_edi_round(
                sum(x['base_amount'] + x['tax_amount'] for x in values_per_grouping_key_total_amount.values())),
            'extraDiscountAmount': 0.0,
            'totalItemsDiscountAmount': 0.0,
        })
        if invoice.ref:
            eta_invoice['purchaseOrderReference'] = invoice.ref
        if invoice.invoice_origin:
            eta_invoice['salesOrderReference'] = invoice.invoice_origin
        return eta_invoice

    @api.model
    def _l10n_eg_eta_prepare_invoice_lines_data(self, invoice, base_lines_aggregated_values):
        lines = []
        totals = {
            'discount_total': 0.0,
            'total_price_subtotal_before_discount' : 0.0,
        }
        for base_line, aggregated_values in base_lines_aggregated_values:
            line = base_line['record']
            tax_details = base_line['tax_details']
            price_unit = self._l10n_eg_edi_round(abs((line.balance / line.quantity) / (1 - (line.discount / 100.0)))) if line.quantity and line.discount != 100.0 else line.price_unit
            price_subtotal_before_discount = self._l10n_eg_edi_round(abs(line.balance / (1 - (line.discount / 100)))) if line.discount != 100.0 else self._l10n_eg_edi_round(price_unit * line.quantity)
            discount_amount = self._l10n_eg_edi_round(price_subtotal_before_discount - abs(line.balance))
            item_code = line.product_id.e_invoicing_code
            lines.append({
                'description': line.name,
                'itemType': item_code.startswith('EG') and 'EGS' or 'GS1',
                'itemCode': item_code,
                'unitType': line.product_uom_id.l10n_eg_unit_code_id.code,
                'quantity': line.quantity,
                'internalCode': line.product_id.default_code or '',
                'valueDifference': 0.0,
                'totalTaxableFees': 0.0,
                'itemsDiscount': 0.0,
                'unitValue': {
                    'currencySold': invoice.currency_id.name,
                    'amountEGP': price_unit,
                },
                'discount': {
                    'rate': line.discount,
                    'amount': discount_amount,
                },
                'taxableItems': [
                    {
                        'taxType': grouping_key['tax_type'],
                        'amount': self._l10n_eg_edi_round(abs(tax_values['tax_amount'])),
                        'subType': grouping_key['sub_type'],
                        'rate': grouping_key['rate'],
                    }
                    for grouping_key, tax_values in aggregated_values.items()
                    if grouping_key
                ],
                'salesTotal': price_subtotal_before_discount,
                'netTotal': self._l10n_eg_edi_round(tax_details['total_excluded'] + tax_details['delta_total_excluded']),
                'total': self._l10n_eg_edi_round(tax_details['total_included']),
            })
            totals['discount_total'] += discount_amount
            totals['total_price_subtotal_before_discount'] += price_subtotal_before_discount
            if invoice.currency_id != self.env.ref('base.EGP'):
                lines[-1]['unitValue']['currencyExchangeRate'] = self._l10n_eg_edi_round(invoice._l10n_eg_edi_exchange_currency_rate())
                lines[-1]['unitValue']['amountSold'] = line.price_unit
        return lines, totals

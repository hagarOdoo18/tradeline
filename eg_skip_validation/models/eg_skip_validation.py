from odoo import api, models, _


class AccountEdiFormat(models.Model):
    _inherit = 'account.edi.format'

    def _check_move_configuration(self, invoice):
        errors = super()._check_move_configuration(invoice)
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

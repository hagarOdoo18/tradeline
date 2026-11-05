"""Keep cached invoice printouts aligned with Ahmed Salama receipt links.

PDF generation is local reporting only. It must never run a sending wizard,
invoke EDI processing, or contact ETA while someone prints an invoice.
"""
from odoo import api, fields, models


class EtaInvoicePdfAttachment(models.Model):
    _inherit = 'ir.attachment'

    eta_invoice_qr_url = fields.Char(copy=False, readonly=True)


class EtaInvoiceQrPrint(models.Model):
    _inherit = 'account.move'

    def _eta_detach_invoice_pdf(self):
        """Preserve previous printouts as attachments; release only their cache role."""
        attachments = self.env['ir.attachment'].sudo().search([
            ('res_model', '=', 'account.move'), ('res_id', 'in', self.ids),
            ('res_field', '=', 'invoice_pdf_report_file'),
        ])
        if not attachments:
            return
        attachments.write({'res_field': False})
        for move in self:
            if move.message_main_attachment_id in attachments:
                move.sudo().message_main_attachment_id = False
        self.invalidate_recordset(['invoice_pdf_report_id', 'invoice_pdf_report_file'])

    def _eta_drop_stale_invoice_pdf(self):
        """Unstamped legacy PDFs are refreshed once, on demand, including old invoices."""
        stale = self.filtered(lambda move: move.state == 'posted'
            and move.move_type in ('out_invoice', 'out_refund') and move.e_invoice_qr_code
            and move.invoice_pdf_report_id
            and move.invoice_pdf_report_id.sudo().eta_invoice_qr_url != move.e_invoice_qr_code)
        stale._eta_detach_invoice_pdf()

    def write(self, vals):
        watched = {'e_invoice_uuid', 'e_invoice_long_id', 'invoice_date'} & vals.keys()
        previous = {move.id: move.e_invoice_qr_code for move in self} if watched else {}
        result = super().write(vals)
        if watched:
            self.filtered(lambda move: previous[move.id] != move.e_invoice_qr_code)._eta_detach_invoice_pdf()
        return result

    def _eta_ensure_qr_invoice_pdf(self):
        """Render the configured invoice report, without submission, signing or email."""
        self.ensure_one()
        self._eta_drop_stale_invoice_pdf()
        if (self.state != 'posted' or self.move_type not in ('out_invoice', 'out_refund')
                or not self.e_invoice_qr_code or self.invoice_pdf_report_id):
            return
        sender = self.env['account.move.send']
        data = {self: {'pdf_report': sender._get_default_pdf_report_id(self)}}
        sender._prepare_invoice_pdf_report(data)
        sender._link_invoice_documents(data)

    def _get_invoice_legal_documents(self, filetype, allow_fallback=False):
        if filetype == 'pdf':
            self._eta_ensure_qr_invoice_pdf()
        return super()._get_invoice_legal_documents(filetype, allow_fallback=allow_fallback)

    def _get_invoice_legal_documents_all(self, allow_fallback=False):
        self._eta_ensure_qr_invoice_pdf()
        return super()._get_invoice_legal_documents_all(allow_fallback=allow_fallback)


class EtaInvoiceQrPdfGeneration(models.AbstractModel):
    _inherit = 'account.move.send'

    @api.model
    def _get_default_mail_attachments_widget(self, move, mail_template, invoice_edi_format=None,
                                             extra_edis=None, pdf_report=None):
        move._eta_drop_stale_invoice_pdf()
        return super()._get_default_mail_attachments_widget(move, mail_template,
            invoice_edi_format=invoice_edi_format, extra_edis=extra_edis, pdf_report=pdf_report)

    @api.model
    def _generate_invoice_documents(self, invoices_data, allow_fallback_pdf=False):
        for move in invoices_data:
            move._eta_drop_stale_invoice_pdf()
        return super()._generate_invoice_documents(invoices_data, allow_fallback_pdf=allow_fallback_pdf)

    @api.model
    def _prepare_invoice_pdf_report(self, invoices_data):
        # Serialize local PDF generation with receipt writes. An approval cannot
        # arrive between rendering a QR-less PDF and installing it as the cache.
        moves = self.env['account.move'].browse(sorted(move.id for move in invoices_data))
        moves.flush_recordset(['e_invoice_uuid', 'e_invoice_long_id', 'invoice_date'])
        if moves:
            self.env.cr.execute('SELECT id FROM account_move WHERE id IN %s ORDER BY id FOR UPDATE',
                                [tuple(moves.ids)])
            moves.invalidate_recordset(['e_invoice_uuid', 'e_invoice_long_id', 'invoice_date', 'e_invoice_qr_code'])
        qr_urls = {move.id: move.e_invoice_qr_code for move in moves}
        result = super()._prepare_invoice_pdf_report(invoices_data)
        for move, data in invoices_data.items():
            if data.get('pdf_attachment_values'):
                data['pdf_attachment_values']['eta_invoice_qr_url'] = qr_urls[move.id] or False
        return result

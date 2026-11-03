"""Direct, independently atomic Odoo-only amendments for a selected invoice list."""
import logging

from odoo import models, _
from odoo.exceptions import AccessError, UserError

from .draft_correction import _VERIFIED_RESET


_logger = logging.getLogger(__name__)


class EtaLocalVatBatch(models.Model):
    _inherit = 'account.move'

    def action_eta_apply_local_vat(self):
        """Start immediately; the client sends bounded direct RPCs, not ETA jobs."""
        self._eta_local_vat_batch_access()
        return {'type': 'ir.actions.client', 'tag': 'tradeline_eta_local_vat_batch',
                'params': {'move_ids': self.ids, 'batch_size': 25}}

    def _eta_local_vat_batch_access(self):
        self.env['res.users']._eta_check_workspace_access()
        if not self.env.user.has_group('account.group_account_invoice'):
            raise AccessError(_('Invoice accounting permissions are required to apply local VAT.'))
        # Do not expose even invoice names from a forged/inaccessible selection.
        self.check_access('read')

    def action_eta_apply_local_vat_batch(self):
        """One bounded transaction; no explicit commits or government requests."""
        self._eta_local_vat_batch_access()
        if len(self) > 25:
            raise UserError(_('Process at most 25 invoices per request. Use Apply 14% VAT to process the full selection automatically.'))
        applied, skipped, failures = [], [], []
        for record in self:
            try:
                with self.env.cr.savepoint():
                    record._eta_local_vat_check_access()
                    move = record.with_company(record.company_id)
                    if move.move_type == 'out_refund':
                        tax = move._eta_local_vat_tax()
                        if not tax:
                            skipped.append(move.id)
                            continue
                        lines_to_update = move.invoice_line_ids.filtered(
                            lambda l: l.product_id
                            and l.product_id.type != 'service'
                            and tax not in l.tax_ids
                        )
                        if not lines_to_update:
                            skipped.append(move.id)
                            continue
                        was_posted = move.state == 'posted'
                        ctx = move.with_context(
                            _eta_verified_reset=_VERIFIED_RESET,
                            _eta_apply_tax=True,
                        )
                        if was_posted:
                            ctx.button_draft()
                        for line in lines_to_update:
                            line.with_context(
                                check_move_validity=False,
                            ).write({'tax_ids': [(4, tax.id)]})
                        if was_posted:
                            ctx.action_post()
                        move.message_post(
                            body=_("Applied 14%% tax to %d credit note line(s).")
                            % len(lines_to_update))
                        applied.append(record.id)
                        continue
                    if move.move_type == 'out_invoice':
                        has_sale = any(
                            l.sale_line_ids for l in move.invoice_line_ids
                        )
                        has_pos = (
                            'pos_order_ids' in move._fields
                            and move.pos_order_ids
                        )
                        if not has_sale and not has_pos:
                            skipped.append(move.id)
                            continue
                    if not move.eta_vat_missing:
                        skipped.append(move.id)
                        continue
                    tax = move._eta_local_vat_tax()
                    move._eta_local_vat_validate(tax)
                    move._eta_apply_local_vat(tax, move._eta_local_vat_preview_digest())
                applied.append(record.id)
            except UserError as exc:
                failures.append((record.id, record.name, str(exc)))
            except Exception:
                # The per-item savepoint has rolled back before continuing.
                # Never claim success for an unexpected preservation failure.
                _logger.exception('Odoo-only VAT batch failed for invoice id=%s', record.id)
                failures.append((record.id, record.name,
                    _('The amendment failed and was rolled back. Contact accounting support.')))
        return self._eta_local_vat_batch_result(len(applied), len(skipped), [
            {'id': item[0], 'invoice': item[1], 'reason': item[2]} for item in failures])

    def button_draft(self):
        """Skip the core EDI lock when called from the apply-tax batch.

        Odoo 18 base account_edi blocks button_draft when an EDI document
        is in 'sent' state.  For a pure accounting tax correction the EDI
        receipt must be preserved, so we temporarily mark 'sent' ETA docs
        as 'to_send', let the base reset proceed (which unlinks to_send
        docs and clears errors), then recreate them as 'sent' so the cron
        never re-submits an already-accepted receipt.
        """
        if self.env.context.get('_eta_apply_tax'):
            eta_sent = self.edi_document_ids.filtered(
                lambda d: d.edi_format_id.code == 'eg_eta'
                and d.state == 'sent')
            # Remember the records so we can restore after the base unlink.
            eta_sent_vals = [{
                'move_id': doc.move_id.id,
                'edi_format_id': doc.edi_format_id.id,
                'attachment_id': doc.attachment_id.id,
                'state': 'sent',
            } for doc in eta_sent]
            # Let base see them as to_send so it does not block.
            if eta_sent:
                eta_sent.write({'state': 'to_send'})
            clean = self.with_context(_eta_apply_tax=False)
            result = super(EtaLocalVatBatch, clean).button_draft()
            # Restore the sent EDI documents the base just unlinked.
            if eta_sent_vals:
                self.env['account.edi.document'].sudo().create(eta_sent_vals)
            return result
        return super().button_draft()

    def _eta_local_vat_batch_result(self, applied, skipped, failures):
        message = _('Applied: %(applied)s. Already has VAT / skipped: %(skipped)s. Blocked: %(blocked)s.',
                    applied=applied, skipped=skipped, blocked=len(failures))
        if failures:
            message += '\n' + '\n'.join('%s: %s' % (item['invoice'], item['reason']) for item in failures[:5])
        params = {'title': _('14% VAT in Odoo only'), 'message': message,
                  'type': 'warning' if failures else 'success', 'sticky': bool(failures),
                  'applied_count': applied, 'skipped_count': skipped, 'blocked_items': failures,
                  'blocked_ids': [item['id'] for item in failures]}
        if failures:
            # The client calls doAction with this explicit action, avoiding
            # unsupported bare model/hash URLs. Ordinary record rules apply.
            params['blocked_action'] = {'type': 'ir.actions.act_window',
                'name': _('Blocked VAT invoices'), 'res_model': 'account.move',
                'view_mode': 'list,form', 'views': [(False, 'list'), (False, 'form')],
                'domain': [('id', 'in', params['blocked_ids'])]}
        return {'type': 'ir.actions.client', 'tag': 'tradeline_eta_local_vat_result', 'params': params}

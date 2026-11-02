"""Direct, independently atomic Odoo-only amendments for a selected invoice list."""
import logging

from odoo import models, _
from odoo.exceptions import AccessError, UserError


_logger = logging.getLogger(__name__)


class EtaLocalVatBatch(models.Model):
    _inherit = 'account.move'

    def action_eta_apply_local_vat(self):
        """Apply locally without a preview, government request, or transaction commit."""
        self.env['res.users']._eta_check_workspace_access()
        if not self.env.user.has_group('account.group_account_invoice'):
            raise AccessError(_('Invoice accounting permissions are required to apply local VAT.'))
        # Do not expose even invoice names from a forged/inaccessible selection.
        self.check_access('read')
        applied, skipped, failures = [], [], []
        for record in self:
            try:
                with self.env.cr.savepoint():
                    record._eta_local_vat_check_access()
                    move = record.with_company(record.company_id)
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
        message = _('Applied: %(applied)s. Already has VAT / skipped: %(skipped)s. Blocked: %(blocked)s.',
                    applied=len(applied), skipped=len(skipped), blocked=len(failures))
        if failures:
            message += '\n' + '\n'.join('%s: %s' % (name, reason) for _id, name, reason in failures[:5])
        params = {'title': _('14% VAT in Odoo only'), 'message': message,
                  'type': 'warning' if failures else 'success', 'sticky': bool(failures),
                  'blocked_ids': [item[0] for item in failures]}
        if failures:
            # The client calls doAction with this explicit action, avoiding
            # unsupported bare model/hash URLs. Ordinary record rules apply.
            params['blocked_action'] = {'type': 'ir.actions.act_window',
                'name': _('Blocked VAT invoices'), 'res_model': 'account.move',
                'view_mode': 'list,form', 'views': [(False, 'list'), (False, 'form')],
                'domain': [('id', 'in', params['blocked_ids'])]}
        return {'type': 'ir.actions.client', 'tag': 'tradeline_eta_local_vat_result', 'params': params}

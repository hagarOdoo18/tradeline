import requests

from odoo import fields, models, _
from odoo.exceptions import UserError


class AccountMove(models.Model):
    _inherit = 'account.move'

    eta_job_ids = fields.One2many('tradeline.eta.job', 'move_id', string='ETA Submission Jobs')

    def _post(self, soft=True):
        posted = super(AccountMove, self.with_context(eta_new_invoice=True))._post(soft=soft)
        eligible = posted.filtered(lambda m: m.move_type in ('out_invoice', 'out_refund'))
        eligible._eta_enqueue()
        return posted

    def _eta_enqueue(self, reconcile=False):
        Job = self.env['tradeline.eta.job'].sudo()
        for move in self.filtered(lambda m: m.state == 'posted' and m.move_type in ('out_invoice', 'out_refund')):
            if not move.company_id.client_id or not move.company_id.client_secret:
                continue
            job = Job.search([('move_id', '=', move.id)], limit=1)
            if not job:
                Job.create({'move_id': move.id, 'state': 'accepted' if move.e_invoice_uuid else
                            'unknown' if reconcile or move.e_invoice_sent else 'queued'})
            elif move.e_invoice_uuid and job.state not in ('valid', 'invalid', 'blocked'):
                job.write({'state': 'accepted'})

    def action_send_electronic_invoice(self):
        """Manual and legacy callers enter the same durable queue as the cron."""
        if any(m.state != 'posted' or m.move_type not in ('out_invoice', 'out_refund') for m in self):
            raise UserError(_('Only posted customer invoices and credit notes can be queued for ETA.'))
        self.check_access('write')
        self._eta_enqueue(reconcile=True)
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'title': _('ETA submission queued'),
            'message': _('Existing ETA IDs will be checked before any submission.'), 'type': 'info',
        }}

    def action_update_electronic_invoice_status(self):
        self.check_access('write')
        self._eta_enqueue()
        jobs = self.env['tradeline.eta.job'].sudo().search([
            ('move_id', 'in', self.ids), ('move_id.e_invoice_uuid', '!=', False)])
        jobs.write({'state': 'accepted', 'next_attempt': False})
        return True

    def cron_send_invoices(self):
        return self.env['tradeline.eta.job']._cron_process('submit')

    def post(self):
        # Legacy entry point must not erase already assigned ETA identifiers.
        return self.action_post()

    def get_signature_value(self):
        """Signing never commits an invoice transaction or releases its locks."""
        self.ensure_one()
        company = self.company_id.sudo()
        if company.signature_tool != 'python':
            raise UserError(_('Automatic ETA signing currently requires the configured Python signer.'))
        if not self.e_invoice_canonical:
            self.with_company(company).action_generate_eta_json()
        try:
            result = requests.get(company.signature_url, data={
                'data': self.e_invoice_canonical, 'token_label': company.signature_label,
                'user_pin': company.signature_pin,
            }, timeout=(5, 25))
            if result.status_code != 200 or not result.text.strip():
                raise UserError(_('The ETA signer did not return a signature (HTTP %s).', result.status_code))
        except requests.RequestException as exc:
            raise UserError(_('The ETA signer is unavailable; the queue will retry.')) from exc
        self.static_signature = result.text.strip()
        return self.static_signature

    def _eta_native_result(self):
        self.ensure_one()
        if self.e_invoice_uuid and self.e_invoice_status == 'Valid':
            return {'success': True}
        if self.e_invoice_uuid and self.e_invoice_status in ('Invalid', 'Rejected', 'Cancelled'):
            return {'error': _('ETA reports %s. Correct the document before any resubmission.', self.e_invoice_status),
                    'blocking_level': 'error'}
        self._eta_enqueue(reconcile=not bool(self.env.context.get('eta_new_invoice')))
        return {'error': _('The Tradeline ETA queue is processing this invoice. See ETA Submission Jobs.'),
                'blocking_level': 'info'}

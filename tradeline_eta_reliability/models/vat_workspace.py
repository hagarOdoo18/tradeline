"""Read-only VAT work routes; never authorize transport or create corrections."""
from odoo import api, fields, models, _
from odoo.tools import SQL


VAT_WORKFLOWS = [
    ('unsent', 'Not sent — fix VAT on this invoice'),
    ('approved', 'Approved without VAT — credit and replacement required'),
    ('in_progress', 'Correction documents already exist — open them'),
    ('rejected', 'ETA invalid — fix this invoice'),
    ('review', 'ETA outcome or receipt needs review'),
    ('completed', 'VAT correction completed'),
]


class EtaVatWorkspace(models.Model):
    _inherit = 'account.move'

    # Nonstored: installing this workspace must never recompute the historical
    # ledger. SQL search returns a permission-scoped subquery, not millions of
    # invoice IDs or a sudo search of the accounting ledger.
    eta_vat_workflow = fields.Selection(VAT_WORKFLOWS, string='VAT action',
        compute='_compute_eta_vat_workflow', compute_sudo=True, search='_search_eta_vat_workflow')
    eta_vat_workflow_note = fields.Text(string='VAT correction instructions',
        compute='_compute_eta_vat_workflow', compute_sudo=True)

    @api.model
    def _eta_flush_vat_workflow(self):
        self.env['account.move'].flush_model([
            'move_type', 'state', 'currency_id', 'e_invoice_status', 'e_invoice_sent',
            'e_invoice_uuid', 'l10n_eg_uuid', 'l10n_eg_submission_number', 'l10n_eg_eta_json_doc_id',
            'e_invoice_json', 'invoice_signed', 'eta_receipt_vat_missing', 'eta_receipt_vat_checked_at',
            'eta_receipt_vat_uuid', 'eta_receipt_vat_egp', 'eta_vat_original_id'])
        self.env['account.move.line'].flush_model(['move_id', 'tax_line_id', 'amount_currency'])
        self.env['tradeline.eta.job'].flush_model([
            'move_id', 'state', 'historical_review', 'attempts', 'attempted_at', 'safe_to_retry',
            'submission_uuid', 'duplicate_retry_at', 'duplicate_payload_hash'])
        self.env['ir.attachment'].flush_model(['res_model', 'res_id', 'name', 'mimetype'])
        self.env['account.edi.document'].flush_model(['move_id', 'edi_format_id', 'state'])
        self.env['account.edi.format'].flush_model(['code'])

    @api.model
    def _eta_vat_workflow_sql(self):
        """One route definition shared by visible-row computation and search."""
        missing = SQL("""NOT EXISTS (
            SELECT 1 FROM account_move_line wf_tax
            WHERE wf_tax.move_id=account_move.id AND wf_tax.tax_line_id IN %s
            GROUP BY wf_tax.move_id HAVING ABS(SUM(wf_tax.amount_currency)) >=
                (SELECT rounding/2 FROM res_currency WHERE id=account_move.currency_id))""",
            tuple(self._eta_vat_taxes().ids) or (0,))
        receipt_zero = SQL("""(
            account_move.state = 'posted'
            AND account_move.eta_receipt_vat_missing IS TRUE
            AND account_move.eta_receipt_vat_checked_at IS NOT NULL
            AND COALESCE(account_move.eta_receipt_vat_egp,0) <= 0
            AND COALESCE(account_move.eta_receipt_vat_uuid,'') <> ''
            AND account_move.eta_receipt_vat_uuid = COALESCE(
                NULLIF(account_move.e_invoice_uuid,''),NULLIF(account_move.l10n_eg_uuid,''))
            AND NOT (COALESCE(account_move.e_invoice_uuid,'') <> ''
                AND COALESCE(account_move.l10n_eg_uuid,'') <> ''
                AND account_move.e_invoice_uuid <> account_move.l10n_eg_uuid))""")
        active = SQL("""EXISTS (SELECT 1 FROM account_move wf_doc
            WHERE wf_doc.eta_vat_original_id=account_move.id AND wf_doc.state <> 'cancel')""")
        completed = SQL("""(
            EXISTS (SELECT 1 FROM account_move wf_credit WHERE wf_credit.eta_vat_original_id=account_move.id
                AND wf_credit.state <> 'cancel' AND wf_credit.move_type='out_refund')
            AND EXISTS (SELECT 1 FROM account_move wf_replacement WHERE wf_replacement.eta_vat_original_id=account_move.id
                AND wf_replacement.state <> 'cancel' AND wf_replacement.move_type='out_invoice')
            AND NOT EXISTS (SELECT 1 FROM account_move wf_pending
                WHERE wf_pending.eta_vat_original_id=account_move.id AND wf_pending.state <> 'cancel'
                    AND wf_pending.move_type IN ('out_invoice','out_refund') AND (
                        wf_pending.state <> 'posted' OR wf_pending.e_invoice_status IS DISTINCT FROM 'Valid'
                        OR COALESCE(wf_pending.e_invoice_uuid,'') = ''
                        OR EXISTS (SELECT 1 FROM tradeline_eta_job wf_pending_job
                            WHERE wf_pending_job.move_id=wf_pending.id
                                AND (wf_pending_job.state IN ('unknown','accepted','review')
                                    OR wf_pending_job.historical_review IS TRUE))
                        OR (wf_pending.move_type='out_invoice' AND (
                            wf_pending.eta_receipt_vat_checked_at IS NULL
                            OR wf_pending.eta_receipt_vat_missing IS TRUE
                            OR COALESCE(wf_pending.eta_receipt_vat_egp,0) <= 0
                            OR wf_pending.eta_receipt_vat_uuid IS DISTINCT FROM wf_pending.e_invoice_uuid
                            OR (COALESCE(wf_pending.l10n_eg_uuid,'') <> ''
                                AND wf_pending.l10n_eg_uuid <> wf_pending.e_invoice_uuid))))))""")
        uncertain = SQL("""(
            (COALESCE(account_move.e_invoice_uuid,'') <> '' AND COALESCE(account_move.l10n_eg_uuid,'') <> ''
                AND account_move.e_invoice_uuid <> account_move.l10n_eg_uuid)
            OR account_move.e_invoice_status IN ('Submitted','Rejected','Cancelled')
            OR EXISTS (SELECT 1 FROM tradeline_eta_job wf_uncertain WHERE wf_uncertain.move_id=account_move.id
                AND (wf_uncertain.state IN ('unknown','accepted','review') OR wf_uncertain.historical_review IS TRUE
                    OR wf_uncertain.duplicate_retry_at IS NOT NULL OR COALESCE(wf_uncertain.duplicate_payload_hash,'') <> ''
                    OR (wf_uncertain.state NOT IN ('valid','invalid')
                        AND (COALESCE(wf_uncertain.attempts,0) > 0 OR wf_uncertain.attempted_at IS NOT NULL)
                        AND wf_uncertain.safe_to_retry IS NOT TRUE))))""")
        # ETA's Rejected status can be a receiver rejection of an earlier
        # approved document. Only Invalid has the existing verified-reset path.
        rejected = SQL("account_move.e_invoice_status = 'Invalid'")
        unsent = SQL("""(
            COALESCE(account_move.e_invoice_uuid,'') = '' AND COALESCE(account_move.l10n_eg_uuid,'') = ''
            AND account_move.e_invoice_sent IS NOT TRUE
            AND COALESCE(account_move.l10n_eg_submission_number,'') = ''
            AND account_move.l10n_eg_eta_json_doc_id IS NULL
            AND COALESCE(account_move.e_invoice_status,'') NOT IN ('Submitted','Valid','Invalid','Rejected','Cancelled')
            AND NOT EXISTS (SELECT 1 FROM tradeline_eta_job wf_sent WHERE wf_sent.move_id=account_move.id
                AND (wf_sent.state NOT IN ('queued','blocked','invalid') OR wf_sent.historical_review IS TRUE
                    OR COALESCE(wf_sent.submission_uuid,'') <> '' OR wf_sent.duplicate_retry_at IS NOT NULL
                    OR COALESCE(wf_sent.duplicate_payload_hash,'') <> ''
                    OR COALESCE(wf_sent.attempts,0) > 0 OR wf_sent.attempted_at IS NOT NULL))
            AND (EXISTS (SELECT 1 FROM tradeline_eta_job wf_owned WHERE wf_owned.move_id=account_move.id)
                OR (COALESCE(account_move.e_invoice_json,'') = '' AND account_move.invoice_signed IS NOT TRUE))
            AND NOT EXISTS (SELECT 1 FROM account_edi_document wf_edi
                JOIN account_edi_format wf_format ON wf_format.id=wf_edi.edi_format_id
                WHERE wf_edi.move_id=account_move.id AND wf_format.code='eg_eta' AND wf_edi.state IN ('sent','to_cancel'))
            AND NOT EXISTS (SELECT 1 FROM ir_attachment wf_archive WHERE wf_archive.res_model='account.move'
                AND wf_archive.res_id=account_move.id AND wf_archive.mimetype='application/json'
                AND wf_archive.name LIKE 'ETA_%%.json'))""")
        return SQL("""CASE
            WHEN account_move.move_type <> 'out_invoice' OR account_move.state = 'cancel'
                OR NOT (%s OR account_move.eta_receipt_vat_missing IS TRUE) THEN NULL
            WHEN %s AND %s AND NOT %s THEN 'completed'
            WHEN %s AND %s THEN 'in_progress'
            WHEN %s THEN 'review'
            WHEN %s AND account_move.e_invoice_status = 'Valid' AND NOT %s THEN 'approved'
            WHEN %s AND NOT %s THEN 'rejected'
            WHEN %s AND NOT %s THEN 'unsent'
            ELSE 'review' END""",
            missing, receipt_zero, completed, uncertain, receipt_zero, active, uncertain,
            receipt_zero, active, rejected, active, unsent, active)

    @api.depends('move_type', 'state', 'eta_vat_missing', 'e_invoice_status', 'e_invoice_sent',
                 'e_invoice_uuid', 'l10n_eg_uuid', 'l10n_eg_submission_number', 'l10n_eg_eta_json_doc_id',
                 'e_invoice_json', 'invoice_signed', 'eta_receipt_vat_missing', 'eta_receipt_vat_checked_at',
                 'eta_receipt_vat_uuid', 'eta_receipt_vat_egp', 'eta_job_ids.state', 'eta_job_ids.historical_review',
                 'eta_job_ids.attempts', 'eta_job_ids.attempted_at', 'eta_job_ids.safe_to_retry',
                 'eta_job_ids.submission_uuid', 'eta_job_ids.duplicate_retry_at', 'eta_job_ids.duplicate_payload_hash',
                 'eta_vat_correction_ids.state', 'eta_vat_correction_ids.move_type',
                 'eta_vat_correction_ids.e_invoice_status', 'eta_vat_correction_ids.e_invoice_uuid',
                 'eta_vat_correction_ids.l10n_eg_uuid', 'eta_vat_correction_ids.eta_receipt_vat_checked_at',
                 'eta_vat_correction_ids.eta_receipt_vat_missing', 'eta_vat_correction_ids.eta_receipt_vat_uuid',
                 'eta_vat_correction_ids.eta_receipt_vat_egp')
    def _compute_eta_vat_workflow(self):
        routes = {}
        saved = self.filtered(lambda move: isinstance(move.id, int))
        if saved:
            self._eta_flush_vat_workflow()
            self.env.cr.execute(SQL('SELECT account_move.id, %s FROM account_move WHERE id IN %s',
                self._eta_vat_workflow_sql(), tuple(saved.ids)))
            routes = dict(self.env.cr.fetchall())
        for move in self:
            route = routes.get(move.id) or False
            if route == 'unsent' and not move._eta_can_reset_unsent():
                route = 'review'
            note = False
            if route == 'unsent':
                note = _('Fix VAT on this existing invoice. If it is posted, Correct VAT reopens its draft safely. No credit note is needed because no submission is recorded.')
            elif route == 'approved':
                note = _('ETA approved the original without VAT. Correct VAT must prepare a linked credit note and a VAT replacement; do not reset or resend the approved original.')
            elif route == 'rejected':
                note = _('ETA marked the original invoice Invalid. Correct VAT verifies its latest outcome before reopening this existing invoice. No credit note is needed for a confirmed invalid submission.')
            elif route == 'in_progress':
                docs = move.eta_vat_correction_ids.filtered(lambda doc: doc.state != 'cancel')
                credits = docs.filtered(lambda doc: doc.move_type == 'out_refund')
                replacements = docs.filtered(lambda doc: doc.move_type == 'out_invoice')
                approved_credit = credits and all(doc.state == 'posted' and doc.e_invoice_status == 'Valid'
                                                  and doc.e_invoice_uuid and doc.eta_job_ids.state == 'valid' for doc in credits)
                if approved_credit and replacements:
                    note = _('The existing credit note is already approved by ETA. Open the existing correction documents and review/post the VAT replacement invoice. Do not create or send another credit note.')
                elif approved_credit:
                    note = _('The existing credit note is already approved by ETA, but no active replacement invoice remains. Accounting must restore the replacement using the existing correction trail. Do not create another credit note.')
                elif credits:
                    note = _('Open the existing correction documents. Review/post the existing credit note first; wait for ETA approval before posting the VAT replacement. Do not prepare duplicate correction documents.')
                else:
                    note = _('Correction documents already exist. Open them and have accounting reconcile the missing credit/replacement links before taking another action.')
            elif route == 'completed':
                note = _('The linked credit and VAT replacement are approved by ETA and positive VAT is verified on the replacement receipt. The original is preserved; no further correction is required.')
            elif route == 'review':
                note = _('Confirm the existing ETA outcome and reconcile its receipt before changing or resending this invoice. An unconfirmed send, conflicting receipts or historical review cannot be treated as an unsent invoice.')
            move.eta_vat_workflow = route
            move.eta_vat_workflow_note = note

    @api.model
    def _search_eta_vat_workflow(self, operator, value):
        if operator not in ('=', '!=', 'in', 'not in'):
            raise ValueError('Use equality or membership for a VAT work route.')
        values = value if operator in ('in', 'not in') else [value]
        if not isinstance(values, (list, tuple, set)):
            raise ValueError('VAT work-route membership needs a list of values.')
        valid = {key for key, label in VAT_WORKFLOWS}
        keys = tuple(key for key in values if key in valid)
        conditions = []
        self._eta_flush_vat_workflow()
        route = self._eta_vat_workflow_sql()
        if keys:
            conditions.append(SQL('%s IN %s', route, keys))
        if False in values:
            conditions.append(SQL('%s IS NULL', route))
        if not conditions:
            domain = [('id', '=', 0)]
        else:
            query = self._search([])
            query.add_where(SQL('(%s)', SQL(' OR ').join(conditions)))
            domain = [('id', 'in', query)]
        return ['!'] + domain if operator in ('!=', 'not in') else domain

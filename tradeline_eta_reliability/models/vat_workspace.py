"""Read-only VAT work routes; never authorize transport or create corrections."""
from odoo import api, fields, models, _
from odoo.tools import SQL


VAT_WORKFLOWS = [
    ('unsent', 'Not sent — fix VAT on this invoice'),
    ('approved', 'ETA approved — add VAT in Odoo only'),
    ('local_completed', 'VAT added in Odoo — ETA unchanged'),
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
            'eta_receipt_vat_uuid', 'eta_receipt_vat_egp', 'eta_vat_original_id',
            'eta_local_vat_applied_at', 'eta_local_vat_applied_by', 'eta_local_vat_audit_id', 'eta_local_vat_receipt_uuid',
            'eta_local_vat_net_amount', 'eta_local_vat_tax_amount', 'eta_local_vat_gross_amount',
            'amount_untaxed', 'amount_tax', 'amount_total', 'eta_submission_pending'])
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
            OR (account_move.e_invoice_status='Valid' AND EXISTS (
                SELECT 1 FROM account_edi_document wf_native_pending
                JOIN account_edi_format wf_native_format ON wf_native_format.id=wf_native_pending.edi_format_id
                WHERE wf_native_pending.move_id=account_move.id AND wf_native_pending.state IN ('to_send','to_cancel')
                    AND wf_native_format.code IS DISTINCT FROM 'eg_eta'))
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
        # Most historical invoices have no local amendment. A CASE is needed:
        # PostgreSQL can reorder AND conditions and otherwise probe tax lines
        # before discovering the cheap NULL audit stamps on every ledger row.
        local_done = SQL("""CASE WHEN account_move.eta_local_vat_applied_at IS NULL
                OR account_move.eta_local_vat_applied_by IS NULL
                OR account_move.eta_local_vat_audit_id IS NULL THEN FALSE
            ELSE (NOT %s AND account_move.state='posted'
            AND account_move.e_invoice_status='Valid' AND account_move.eta_submission_pending IS NOT TRUE
            AND account_move.eta_vat_original_id IS NULL
            AND NOT EXISTS (SELECT 1 FROM account_move wf_local_credit WHERE wf_local_credit.state <> 'cancel'
                AND (wf_local_credit.eta_vat_original_id=account_move.id OR wf_local_credit.reversed_entry_id=account_move.id))
            AND NOT EXISTS (SELECT 1 FROM tradeline_eta_job wf_local_job WHERE wf_local_job.move_id=account_move.id
                AND (wf_local_job.state <> 'valid' OR wf_local_job.historical_review IS TRUE
                    OR wf_local_job.duplicate_retry_at IS NOT NULL OR COALESCE(wf_local_job.duplicate_payload_hash,'') <> ''))
            AND NOT EXISTS (SELECT 1 FROM account_edi_document wf_local_edi
                JOIN account_edi_format wf_local_format ON wf_local_format.id=wf_local_edi.edi_format_id
                WHERE wf_local_edi.move_id=account_move.id AND wf_local_edi.state IN ('to_send','to_cancel')
                    AND wf_local_format.code IS DISTINCT FROM 'eg_eta')
            AND ROUND(account_move.eta_local_vat_net_amount / (SELECT rounding FROM res_currency WHERE id=account_move.currency_id))
                = ROUND(account_move.amount_untaxed / (SELECT rounding FROM res_currency WHERE id=account_move.currency_id))
            AND ROUND(account_move.eta_local_vat_tax_amount / (SELECT rounding FROM res_currency WHERE id=account_move.currency_id))
                = ROUND(account_move.amount_tax / (SELECT rounding FROM res_currency WHERE id=account_move.currency_id))
            AND ROUND(account_move.eta_local_vat_gross_amount / (SELECT rounding FROM res_currency WHERE id=account_move.currency_id))
                = ROUND(account_move.amount_total / (SELECT rounding FROM res_currency WHERE id=account_move.currency_id))
            AND account_move.eta_local_vat_applied_at IS NOT NULL
            AND account_move.eta_local_vat_applied_by IS NOT NULL
            AND COALESCE(account_move.eta_local_vat_receipt_uuid,'') <> ''
            AND account_move.eta_local_vat_receipt_uuid = COALESCE(
                NULLIF(account_move.e_invoice_uuid,''),NULLIF(account_move.l10n_eg_uuid,''))
            AND NOT (COALESCE(account_move.e_invoice_uuid,'') <> ''
                AND COALESCE(account_move.l10n_eg_uuid,'') <> ''
                AND account_move.e_invoice_uuid <> account_move.l10n_eg_uuid)
            AND EXISTS (SELECT 1 FROM ir_attachment wf_local
                WHERE wf_local.id=account_move.eta_local_vat_audit_id
                    AND wf_local.res_model='account.move' AND wf_local.res_id=account_move.id
                    AND wf_local.company_id=account_move.company_id
                    AND wf_local.res_field='eta_local_vat_audit_file' AND wf_local.public IS NOT TRUE)) END""", missing)
        return SQL("""CASE
            WHEN account_move.move_type <> 'out_invoice' OR account_move.state = 'cancel' THEN NULL
            WHEN %s THEN 'local_completed'
            WHEN NOT (%s OR account_move.eta_receipt_vat_missing IS TRUE) THEN NULL
            WHEN %s AND %s AND NOT %s THEN 'completed'
            WHEN %s AND %s THEN 'in_progress'
            WHEN %s THEN 'review'
            WHEN %s AND account_move.e_invoice_status = 'Valid' AND NOT %s THEN 'approved'
            WHEN %s AND NOT %s THEN 'rejected'
            WHEN %s AND NOT %s THEN 'unsent'
            ELSE 'review' END""",
            local_done, missing, receipt_zero, completed, uncertain, receipt_zero, active, uncertain,
            receipt_zero, active, rejected, active, unsent, active)

    @api.depends('move_type', 'state', 'eta_vat_missing', 'e_invoice_status', 'e_invoice_sent',
                 'e_invoice_uuid', 'l10n_eg_uuid', 'l10n_eg_submission_number', 'l10n_eg_eta_json_doc_id',
                 'e_invoice_json', 'invoice_signed', 'eta_receipt_vat_missing', 'eta_receipt_vat_checked_at',
                 'eta_receipt_vat_uuid', 'eta_receipt_vat_egp', 'eta_job_ids.state', 'eta_job_ids.historical_review',
                 'eta_job_ids.attempts', 'eta_job_ids.attempted_at', 'eta_job_ids.safe_to_retry',
                 'eta_job_ids.submission_uuid', 'eta_job_ids.duplicate_retry_at', 'eta_job_ids.duplicate_payload_hash',
                 'eta_local_vat_applied_at', 'eta_local_vat_applied_by', 'eta_local_vat_audit_id',
                 'eta_local_vat_receipt_uuid', 'eta_local_vat_net_amount', 'eta_local_vat_tax_amount',
                 'eta_local_vat_gross_amount', 'amount_untaxed', 'amount_tax', 'amount_total',
                 'eta_submission_pending', 'reversal_move_ids.state', 'edi_document_ids.state', 'edi_document_ids.edi_format_id.code', 'eta_vat_correction_ids.state', 'eta_vat_correction_ids.move_type',
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
                note = _('ETA approved the original without VAT. Click Add 14% VAT in Odoo, review the unchanged customer total, then apply. This changes Odoo accounting only; no ETA credit, replacement or resend is created.')
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
            elif route == 'local_completed':
                note = _('14% VAT was added in Odoo. The customer total and ETA receipt were preserved. No further ETA action is needed for this local amendment.')
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
            # These are necessary conditions, not alternate route definitions.
            # Bound the expensive correlated expression before looking at the
            # historical ledger. Empty-route membership must retain all rows.
            scope = []
            if False not in values:
                scope = [('move_type', '=', 'out_invoice'), ('state', '!=', 'cancel')]
                if set(keys) == {'approved'}:
                    scope += [('state', '=', 'posted'), ('e_invoice_status', '=', 'Valid'),
                              ('eta_receipt_vat_missing', '=', True)]
                elif set(keys) == {'local_completed'}:
                    scope += [('eta_local_vat_applied_at', '!=', False)]
            query = self._search(scope)
            query.add_where(SQL('(%s)', SQL(' OR ').join(conditions)))
            domain = [('id', 'in', query)]
        return ['!'] + domain if operator in ('!=', 'not in') else domain

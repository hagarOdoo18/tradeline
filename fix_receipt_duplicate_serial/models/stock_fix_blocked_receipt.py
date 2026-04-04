# -*- coding: utf-8 -*-
import logging
from collections import defaultdict
from odoo import api, models, _

_logger = logging.getLogger(__name__)


class StockFixBlockedReceipt(models.AbstractModel):
    """
    Cron job that fixes receipts (stock.picking) that are blocked from
    validation because they contain duplicate serial numbers in their
    stock.move.line records.

    Root cause
    ----------
    Odoo raises a validation error when two move lines in the same picking
    share the same lot_id (serial number).  This can happen when:
      - A barcode was scanned twice by mistake.
      - An import / EDI loaded the same serial twice.
      - A POS or external system pushed duplicate lines.

    Fix strategy (per duplicate group inside a picking)
    ---------------------------------------------------
    1. Keep the move line with the highest qty_done (or the first one if tied).
    2. For every other (duplicate) line in that group:
         a. If qty_done == 0  →  unlink (delete) the line.
         b. If qty_done  > 0  →  clear its lot_id so the picking can be
                                   saved; the operator will re-assign the
                                   serial manually.
    3. Re-check:  after the fix the picking must have no remaining duplicate
       lot_ids before we try to validate it.
    4. Optionally auto-validate the picking if all lines are filled
       (controlled by the class attribute AUTO_VALIDATE).

    All actions are logged to _logger and ir.logging for full traceability.
    """

    _name = 'stock.fix.blocked.receipt'
    _description = 'Fix Blocked Receipts — Duplicate Serial Numbers'

    # Set to True if you want the cron to auto-validate the picking after
    # fixing the duplicates (only when every line is fully filled).
    AUTO_VALIDATE = True

    # ------------------------------------------------------------------ #
    #  CRON ENTRY POINT                                                    #
    # ------------------------------------------------------------------ #

    @api.model
    def cron_fix_blocked_receipts(self):
        _logger.info("=== [fix_blocked_receipt] Cron started ===")

        blocked = self._find_blocked_receipts()

        if not blocked:
            _logger.info("[fix_blocked_receipt] No blocked receipts found.")
            return

        _logger.warning(
            "[fix_blocked_receipt] Found %d blocked receipt(s). Processing...",
            len(blocked),
        )

        report = []
        for picking in blocked:
            result = self._fix_picking(picking)
            report.append(result)

        fixed     = sum(1 for r in report if r['status'] == 'fixed')
        validated = sum(1 for r in report if r['status'] == 'validated')
        skipped   = sum(1 for r in report if r['status'] == 'needs_review')

        summary = (
            f"[fix_blocked_receipt] Done — "
            f"Receipts processed: {len(report)} | "
            f"Fixed (not validated): {fixed} | "
            f"Fixed + validated: {validated} | "
            f"Skipped (manual review): {skipped}"
        )
        _logger.warning(summary)
        self._log_to_ir_logging(summary)
        self._notify_managers(report)

        _logger.info("=== [fix_blocked_receipt] Cron finished ===")

    # ------------------------------------------------------------------ #
    #  STEP 1 — FIND BLOCKED RECEIPTS                                      #
    # ------------------------------------------------------------------ #

    @api.model
    def _find_blocked_receipts(self):
        """
        Return pickings that:
          - Are incoming receipts (picking_type_code = 'incoming')
          - Are in state 'assigned' or 'confirmed'  (not yet validated)
          - Have at least one serial-tracked product with a duplicate lot_id
            within the same picking.
        """
        # Candidate pickings: ready or confirmed incoming receipts
        candidates = self.env['stock.picking'].sudo().search([
            ('picking_type_code', '=', 'incoming'),
            ('state', 'in', ['confirmed', 'assigned']),
        ])

        blocked = self.env['stock.picking']
        for picking in candidates:
            if self._has_duplicate_serials(picking):
                blocked |= picking
                _logger.warning(
                    "[find] Blocked receipt: %s (id=%d) — has duplicate serials.",
                    picking.name, picking.id,
                )

        return blocked

    @api.model
    def _has_duplicate_serials(self, picking):
        """Return True if any lot_id appears more than once in the picking's move lines."""
        serial_lines = picking.move_line_ids.filtered(
            lambda l: l.lot_id and l.product_id.tracking == 'serial'
        )
        lot_ids = serial_lines.mapped('lot_id').ids
        return len(lot_ids) != len(set(lot_ids))

    # ------------------------------------------------------------------ #
    #  STEP 2 — FIX A SINGLE PICKING                                       #
    # ------------------------------------------------------------------ #

    @api.model
    def _fix_picking(self, picking):
        """
        Fix duplicate serials inside one picking.
        Returns a dict with keys: picking, status, detail.
        """
        _logger.info(
            "[fix] Processing picking '%s' (id=%d) ...",
            picking.name, picking.id,
        )

        serial_lines = picking.move_line_ids.filtered(
            lambda l: l.lot_id and l.product_id.tracking == 'serial'
        )

        # Group move lines by (lot_id) — we want groups with > 1 line
        groups = defaultdict(lambda: self.env['stock.move.line'])
        for ml in serial_lines:
            groups[ml.lot_id.id] |= ml

        duplicates = {lot_id: lines for lot_id, lines in groups.items() if len(lines) > 1}

        cleared_lots  = []   # lot names whose extra lines were cleared
        deleted_lines = 0    # count of deleted zero-qty lines

        for lot_id, lines in duplicates.items():
            lot = self.env['stock.lot'].browse(lot_id)

            # Sort: keep the line with the highest qty_done (if tied, keep first)
            sorted_lines = lines.sorted(key=lambda l: l.qty_done, reverse=True)
            keeper       = sorted_lines[0]
            extras       = sorted_lines[1:]

            _logger.warning(
                "[fix] Picking '%s' | serial '%s' appears %d times. "
                "Keeping move_line id=%d (qty_done=%.2f). Fixing %d extra line(s).",
                picking.name, lot.name, len(lines),
                keeper.id, keeper.qty_done, len(extras),
            )

            for ml in extras:
                if ml.qty_done == 0:
                    # Safe to delete — no quantity was recorded on this line
                    _logger.info(
                        "[fix] Deleting empty duplicate line id=%d (lot='%s', qty_done=0).",
                        ml.id, lot.name,
                    )
                    ml.sudo().unlink()
                    deleted_lines += 1
                else:
                    # Line has a qty_done — clear the lot so the picking
                    # can be saved; operator must re-assign serial manually.
                    _logger.warning(
                        "[fix] Clearing lot on line id=%d (lot='%s', qty_done=%.2f) "
                        "— operator must re-assign manually.",
                        ml.id, lot.name, ml.qty_done,
                    )
                    ml.sudo().write({'lot_id': False})
                    cleared_lots.append(lot.name)

        # Re-check: are there still duplicates after the fix?
        if self._has_duplicate_serials(picking):
            _logger.error(
                "[fix] Picking '%s' still has duplicates after fix attempt. "
                "Skipping — manual review required.",
                picking.name,
            )
            return {
                'picking': picking,
                'status':  'needs_review',
                'detail':  'Duplicates remain after automatic fix.',
            }

        detail_parts = []
        if deleted_lines:
            detail_parts.append(f"{deleted_lines} empty line(s) deleted")
        if cleared_lots:
            detail_parts.append(
                f"lot cleared on {len(cleared_lots)} line(s) "
                f"({', '.join(set(cleared_lots))}) — needs manual re-assignment"
            )
        detail = '; '.join(detail_parts) or 'No changes made'

        # Optionally auto-validate if every line is fully filled
        if self.AUTO_VALIDATE and self._is_fully_filled(picking):
            try:
                picking.sudo().with_context(skip_immediate=True).button_validate()
                _logger.info(
                    "[fix] Picking '%s' auto-validated successfully.", picking.name,
                )
                return {'picking': picking, 'status': 'validated', 'detail': detail}
            except Exception as exc:
                _logger.error(
                    "[fix] Auto-validate failed for '%s': %s", picking.name, str(exc),
                )

        _logger.info("[fix] Picking '%s' fixed. %s", picking.name, detail)
        return {'picking': picking, 'status': 'fixed', 'detail': detail}

    @staticmethod
    def _is_fully_filled(picking):
        """Return True if every move line has qty_done > 0 and a lot assigned."""
        return all(
            ml.qty_done > 0 and (
                ml.product_id.tracking != 'serial' or ml.lot_id
            )
            for ml in picking.move_line_ids
        )

    # ------------------------------------------------------------------ #
    #  UTILITIES                                                           #
    # ------------------------------------------------------------------ #

    @api.model
    def _log_to_ir_logging(self, message):
        self.env['ir.logging'].sudo().create({
            'name':    'fix_blocked_receipt',
            'type':    'server',
            'level':   'WARNING',
            'dbname':  self.env.cr.dbname,
            'message': message,
            'path':    'stock.fix.blocked.receipt',
            'func':    'cron_fix_blocked_receipts',
            'line':    '0',
        })

    @api.model
    def _notify_managers(self, report):
        status_color = {
            'fixed':        '#27ae60',
            'validated':    '#2980b9',
            'needs_review': '#e74c3c',
        }

        rows = ''.join(
            f"""
            <tr>
              <td style="padding:5px 8px;border:1px solid #ddd;">
                <a href="/web#id={r['picking'].id}&model=stock.picking&view_type=form">
                  {r['picking'].name}
                </a>
              </td>
              <td style="padding:5px 8px;border:1px solid #ddd;">
                {r['picking'].partner_id.name or '—'}
              </td>
              <td style="padding:5px 8px;border:1px solid #ddd;">
                <span style="color:{status_color.get(r['status'],'#333')};
                    font-weight:bold;">
                  {r['status'].replace('_',' ').upper()}
                </span>
              </td>
              <td style="padding:5px 8px;border:1px solid #ddd;font-size:12px;">
                {r['detail']}
              </td>
            </tr>
            """
            for r in report
        )

        fixed     = sum(1 for r in report if r['status'] == 'fixed')
        validated = sum(1 for r in report if r['status'] == 'validated')
        skipped   = sum(1 for r in report if r['status'] == 'needs_review')

        body = f"""
        <h3 style="color:#c0392b;">
          ⚠️ Blocked Receipts — Duplicate Serial Number Fix Report
        </h3>
        <p>
          <b>{len(report)}</b> blocked receipt(s) processed by the cron job:<br/>
          <span style="color:#27ae60;"><b>{fixed}</b> fixed</span> &nbsp;|&nbsp;
          <span style="color:#2980b9;"><b>{validated}</b> fixed &amp; auto-validated</span> &nbsp;|&nbsp;
          <span style="color:#e74c3c;"><b>{skipped}</b> need manual review</span>
        </p>
        <table style="border-collapse:collapse;width:100%;font-size:13px;">
          <thead style="background:#f5f5f5;">
            <tr>
              <th style="padding:6px 8px;border:1px solid #ddd;">Receipt</th>
              <th style="padding:6px 8px;border:1px solid #ddd;">Vendor</th>
              <th style="padding:6px 8px;border:1px solid #ddd;">Status</th>
              <th style="padding:6px 8px;border:1px solid #ddd;">Detail</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
        <br/>
        <p style="background:#fef9e7;padding:10px;border-left:4px solid #f39c12;">
          <b>Lines marked "needs manual re-assignment"</b> had a recorded
          quantity but a duplicated serial. The serial was <i>cleared</i> from
          the extra line — please open that receipt, assign the correct serial
          to the cleared line, then validate manually.
        </p>
        <p style="color:#aaa;font-size:11px;">
          Generated by <i>Fix Blocked Receipts</i> cron job.
        </p>
        """

        inventory_group = self.env.ref(
            'stock.group_stock_manager', raise_if_not_found=False
        )
        if not inventory_group:
            return

        partner_ids = inventory_group.users.mapped('partner_id').ids
        if not partner_ids:
            return

        try:
            self.env['mail.thread'].sudo().message_notify(
                partner_ids=partner_ids,
                subject=_('[ACTION NEEDED] Blocked Receipts Fixed — Duplicate Serials'),
                body=body,
                message_type='email',
                subtype_xmlid='mail.mt_comment',
            )
        except Exception as exc:
            _logger.error(
                "[fix_blocked_receipt] Email notification failed: %s", str(exc)
            )

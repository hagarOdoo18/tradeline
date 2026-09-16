# -*- coding: utf-8 -*-
import logging
from collections import defaultdict
from odoo import api, models, _

_logger = logging.getLogger(__name__)


class StockFixBlockedReceipt(models.AbstractModel):
    """
    Cron job that fixes stock.picking records linked to POS orders where
    the move lines are missing serial numbers (lot_id = False).

    The correct serials are already recorded on pos.order.line.lot_id.
    This cron copies them to the corresponding picking move lines,
    skipping any serial that would create a duplicate within the same picking.

    Fix strategy
    ------------
    1. Find all open pickings (confirmed/assigned) linked to POS orders.
    2. For each picking, find move lines where lot_id is not set
       (serial not yet assigned).
    3. For each such line, look up the matching pos.order.line by
       (product_id) and read its lot_id.
    4. Assign the serial to the move line ONLY if that serial is not
       already used in another line of the same picking (no duplicate).
    5. If a serial from POS is already used in the picking → skip it
       and log a warning so the operator can assign manually.
    6. Optionally auto-validate if all lines are filled after the fix.
    """

    _name = 'stock.fix.blocked.receipt'
    _description = 'Fix Blocked Receipts — Assign Serials from POS Lines'

    AUTO_VALIDATE = True

    # ------------------------------------------------------------------ #
    #  CRON ENTRY POINT                                                    #
    # ------------------------------------------------------------------ #

    @api.model
    def cron_fix_blocked_receipts(self):
        _logger.info("=== [fix_blocked_receipt] Cron started ===")

        pickings = self._find_blocked_receipts()

        if not pickings:
            _logger.info("[fix_blocked_receipt] No pickings need serial assignment.")
            return

        _logger.warning(
            "[fix_blocked_receipt] Found %d picking(s) with unassigned serials.",
            len(pickings),
        )

        report = []
        for picking in pickings:
            result = self._fix_picking(picking)
            report.append(result)

        fixed     = sum(1 for r in report if r['status'] == 'fixed')
        validated = sum(1 for r in report if r['status'] == 'validated')
        skipped   = sum(1 for r in report if r['status'] == 'needs_review')

        summary = (
            f"[fix_blocked_receipt] Done — "
            f"Pickings processed: {len(report)} | "
            f"Fixed: {fixed} | "
            f"Fixed + validated: {validated} | "
            f"Needs review: {skipped}"
        )
        _logger.warning(summary)
        self._log_to_ir_logging(summary)
        self._notify_managers(report)

        _logger.info("=== [fix_blocked_receipt] Cron finished ===")

    # ------------------------------------------------------------------ #
    #  STEP 1 — FIND PICKINGS THAT NEED SERIAL ASSIGNMENT                  #
    # ------------------------------------------------------------------ #

    @api.model
    def _find_blocked_receipts(self):
        """
        Return open pickings linked to POS orders that have at least one
        serial-tracked move line with lot_id = False (serial not assigned).

        Guards
        ------
        - Returns empty recordset if POS is not installed.
        - Only looks at pickings in state confirmed or assigned.
        - Only considers products with tracking = 'serial'.
        """
        PosOrderLine = self.env.get('pos.order.line')
        if PosOrderLine is None:
            _logger.info("[find] POS module not installed — nothing to scan.")
            return self.env['stock.picking']

        # Collect all open pickings linked to POS orders via picking_ids
        pos_orders = self.env['pos.order'].sudo().search([
            ('state', 'in', ['paid', 'done', 'invoiced'])
        ])
        if 'refunded_order_id' in pos_orders._fields:
            pos_orders_refunded = pos_orders.filtered('refunded_order_id')
        if not pos_orders_refunded:
            _logger.info("[find] No open POS-linked pickings found.")
            return self.env['stock.picking']

        candidates = self.env['stock.picking']
        for order in pos_orders_refunded:
            for picking in order.picking_ids.sudo().filtered(
                    lambda p: p.state in ('confirmed', 'assigned')
            ):
                if picking not in candidates:
                    candidates |= picking

        if not candidates:
            _logger.info("[find] No open POS-linked pickings found.")
            return self.env['stock.picking']

        # Keep only pickings that have at least one serial line without a lot
        needs_fix = self.env['stock.picking']
        for picking in candidates:
            missing = picking.move_line_ids.filtered(
                lambda l: l.product_id.tracking == 'serial' and not l.lot_id
            )
            missing_move = picking.move_ids.filtered(
                lambda l: l.product_uom_qty != l.quantity
            )

            for m in missing_move:
                m.sudo().write({'quantity': m.product_uom_qty})

            if missing:
                needs_fix |= picking
                _logger.warning(
                    "[find] Picking %s (id=%d) has %d move line(s) "
                    "with no serial assigned.",
                    picking.name, picking.id, len(missing),
                )
            else:
                try:
                    picking.sudo().with_context(skip_immediate=True).button_validate()
                    _logger.info(
                        "[fix] Picking '%s' auto-validated successfully.", picking.name,
                    )
                except Exception as exc:
                    _logger.error(
                        "[fix] Auto-validate failed for '%s': %s",
                        picking.name, str(exc),
                    )
        return needs_fix

    # ------------------------------------------------------------------ #
    #  STEP 2 — ASSIGN SERIALS FROM POS LINES TO MOVE LINES               #
    # ------------------------------------------------------------------ #

    @api.model
    def _fix_picking(self, picking):
        """
        For each move line in the picking that has no lot_id:
          1. Find the linked POS order for this picking.
          2. Look up pos.order.line by product_id to get the serial (lot_id).
          3. Check that serial is not already used in this picking.
          4. If clean → assign it to the move line.
          5. If duplicate → skip and log for manual review.

        Returns dict with keys: picking, status, detail, skipped_serials.
        """
        _logger.info(
            "[fix] Processing picking '%s' (id=%d) ...",
            picking.name, picking.id,
        )

        # Get the POS order linked to this picking
        pos_order = self._get_pos_order(picking)
        if not pos_order:
            _logger.warning(
                "[fix] Could not find POS order for picking '%s'. Skipping.",
                picking.name,
            )
            return {
                'picking':         picking,
                'status':          'needs_review',
                'detail':          'No linked POS order found.',
                'skipped_serials': [],
            }

        # Build a map: product_id -> [lot_id, ...] from POS order lines
        # A POS order can have multiple lines for the same product with
        # different serials, so we collect them all in order.
        pos_serials = self._get_pos_serials_map(pos_order)

        if not pos_serials:
            _logger.warning(
                "[fix] POS order %s has no serial-tracked lines. Skipping.",
                pos_order.name,
            )
            return {
                'picking':         picking,
                'status':          'needs_review',
                'detail':          'POS order has no serial-tracked lines.',
                'skipped_serials': [],
            }

        # Track serials already used in this picking (to prevent duplicates)
        used_lot_ids = set(
            picking.move_line_ids.filtered(lambda l: l.lot_id).mapped('lot_id').ids
        )

        assigned      = []   # (line_id, lot_name) successfully assigned
        skipped_serials = []  # lot names skipped due to duplicate risk

        # Find move lines that still need a serial
        missing_lines = picking.move_line_ids.filtered(
            lambda l: l.product_id.tracking == 'serial' and not l.lot_id
        )

        for ml in missing_lines:
            product_id = ml.product_id.id

            # Pop the next available serial for this product from POS
            available = [
                lot_id for lot_id in pos_serials.get(product_id, [])
                if lot_id not in used_lot_ids
            ]

            if not available:
                lot_name = self._first_pos_lot_name(pos_serials, product_id)
                _logger.warning(
                    "[fix] Picking '%s' | product '%s' | move_line id=%d — "
                    "no available (non-duplicate) serial from POS order %s. "
                    "Needs manual assignment.",
                    picking.name, ml.product_id.display_name,
                    ml.id, pos_order.name,
                )
                skipped_serials.append(lot_name or '?')
                continue

            # Take the first available serial and assign it
            lot_id = available[0]
            lot    = self.env['stock.lot'].browse(lot_id)

            _logger.info(
                "[fix] Assigning serial '%s' to move_line id=%d "
                "(product='%s', picking='%s').",
                lot.name, ml.id, ml.product_id.display_name, picking.name,
            )

            ml.sudo().write({'lot_id': lot_id})
            ml.move_id.quantity = ml.move_id.product_uom_qty
            used_lot_ids.add(lot_id)   # mark as used so next line won't reuse it
            assigned.append((ml.id, lot.name))

        # Build detail string
        detail_parts = []
        if assigned:
            detail_parts.append(
                f"{len(assigned)} serial(s) assigned: "
                + ', '.join(name for _, name in assigned)
            )
        if skipped_serials:
            detail_parts.append(
                f"{len(skipped_serials)} serial(s) skipped (duplicate/missing): "
                + ', '.join(set(skipped_serials))
            )
        detail = '; '.join(detail_parts) or 'No changes made'

        # Auto-validate if enabled and all lines are now filled
        if self.AUTO_VALIDATE and self._is_fully_filled(picking):
            try:
                picking.sudo().with_context(skip_immediate=True).button_validate()
                _logger.info(
                    "[fix] Picking '%s' auto-validated successfully.", picking.name,
                )
                return {
                    'picking':         picking,
                    'status':          'validated',
                    'detail':          detail,
                    'skipped_serials': skipped_serials,
                }
            except Exception as exc:
                _logger.error(
                    "[fix] Auto-validate failed for '%s': %s",
                    picking.name, str(exc),
                )

        status = 'needs_review' if skipped_serials else 'fixed'
        _logger.info("[fix] Picking '%s' → %s. %s", picking.name, status, detail)
        return {
            'picking':         picking,
            'status':          status,
            'detail':          detail,
            'skipped_serials': skipped_serials,
        }

    # ------------------------------------------------------------------ #
    #  HELPERS                                                             #
    # ------------------------------------------------------------------ #

    @api.model
    def _get_pos_order(self, picking):
        """
        Return the pos.order linked to this picking.
        Tries picking.pos_order_id first (Odoo 17/18),
        then searches pos.order.picking_ids as fallback.
        """
        # Odoo 17/18 direct field
        if 'pos_order_id' in picking._fields and picking.pos_order_id:
            return picking.pos_order_id

        # Fallback: search pos.order that references this picking
        PosOrder = self.env.get('pos.order')
        if PosOrder is None:
            return None

        order = PosOrder.sudo().search(
            [('picking_ids', 'in', picking.id)], limit=1
        )
        return order or None

    @api.model
    def _get_pos_serials_map(self, pos_order):
        """
        Return a dict {product_id: [lot_id, lot_id, ...]} from the
        pos.order.line records that have a lot_id set.
        Multiple lines for the same product each contribute one lot_id.
        """
        serial_map = defaultdict(list)
        for line in pos_order.lines:
            if line.pack_lot_ids and line.product_id.tracking == 'serial':
                for lot in line.pack_lot_ids:
                    stock_lot_id = self.env['stock.lot'].search([('name', '=', lot.lot_name)], limit=1)
                    if stock_lot_id.id not in serial_map[line.product_id.id]:
                        serial_map[line.product_id.id].append(stock_lot_id.id)
        return dict(serial_map)

    @staticmethod
    def _first_pos_lot_name(pos_serials, product_id):
        """Return the name of the first lot for a product (for logging)."""
        lots = pos_serials.get(product_id, [])
        return str(lots[0]) if lots else None

    @staticmethod
    def _is_fully_filled(picking):
        """Return True if every serial-tracked move line has qty_done > 0 and lot_id set."""
        return all(
            ml.qty_done > 0 and (
                ml.product_id.tracking != 'serial' or ml.lot_id
            )
            for ml in picking.move_line_ids
        )

    # ------------------------------------------------------------------ #
    #  UTILITIES — LOGGING & NOTIFICATION                                  #
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
                <span style="color:{status_color.get(r['status'], '#333')};
                    font-weight:bold;">
                  {r['status'].replace('_', ' ').upper()}
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
        <h3 style="color:#2471a3;">
          📋 POS Serial Assignment Report — Blocked Receipts
        </h3>
        <p>
          <b>{len(report)}</b> picking(s) processed:<br/>
          <span style="color:#27ae60;"><b>{fixed}</b> fixed</span> &nbsp;|&nbsp;
          <span style="color:#2980b9;"><b>{validated}</b> fixed &amp; auto-validated</span> &nbsp;|&nbsp;
          <span style="color:#e74c3c;"><b>{skipped}</b> need manual review</span>
        </p>
        <table style="border-collapse:collapse;width:100%;font-size:13px;">
          <thead style="background:#f5f5f5;">
            <tr>
              <th style="padding:6px 8px;border:1px solid #ddd;">Picking</th>
              <th style="padding:6px 8px;border:1px solid #ddd;">Customer</th>
              <th style="padding:6px 8px;border:1px solid #ddd;">Status</th>
              <th style="padding:6px 8px;border:1px solid #ddd;">Detail</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
        <br/>
        <p style="background:#fef9e7;padding:10px;border-left:4px solid #f39c12;">
          <b>Lines marked NEEDS REVIEW</b> could not be assigned a serial
          automatically because either no serial was found on the POS order line,
          or all available serials for that product were already used in the same
          picking. Please open those pickings and assign the serial manually.
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
                subject=_('[ACTION NEEDED] POS Serial Assignment — Blocked Receipts'),
                body=body,
                message_type='email',
                subtype_xmlid='mail.mt_comment',
            )
        except Exception as exc:
            _logger.error(
                "[fix_blocked_receipt] Email notification failed: %s", str(exc)
            )

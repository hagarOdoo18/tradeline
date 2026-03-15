"""
One-time backfill for internal transfer return flow.

Goal:
- Find historical internal returns.
- Resolve their original pickings.
- Find "next transfer(s)" for each original using the same logic as runtime:
  1) request-linked chain first
  2) fallback to origin token chain
- Cancel pending next transfer(s) that should have been canceled.

Safety:
- DRY_RUN = True by default (no writes).
- Skips originals where any next transfer is already done.
- Prints a clear summary and details.

Run in Odoo shell:
    exec(open('/home/odoo/backfill_cancel_next_transfers.py', encoding='utf-8').read())
or:
    exec(open('d:/tradline/.tmp/backfill_cancel_next_transfers.py', encoding='utf-8').read())
"""

from collections import defaultdict


# ---------------------------
# Config
# ---------------------------
DRY_RUN = True
ONLY_COMPANY_ID = None  # e.g. 1
MAX_ORIGINALS = None    # e.g. 500

PENDING_STATES = {"draft", "waiting", "confirmed", "assigned", "partially_available"}


def _origin_tokens(origin_value):
    return [token.strip() for token in (origin_value or "").split(",") if token.strip()]


def _get_next_request_transfers(picking):
    if not picking.request_id or picking.picking_type_code != "internal":
        return env["stock.picking"]
    return picking.request_id.transfer_ids.filtered(
        lambda transfer: transfer.id != picking.id
        and transfer.picking_type_code == "internal"
        and transfer.location_id.id == picking.location_dest_id.id
    )


def _get_next_origin_transfers(picking):
    if picking.picking_type_code != "internal":
        return env["stock.picking"]
    domain = [
        ("id", "!=", picking.id),
        ("company_id", "=", picking.company_id.id),
        ("picking_type_code", "=", "internal"),
        ("location_id", "=", picking.location_dest_id.id),
        ("origin", "!=", False),
    ]
    candidates = env["stock.picking"].sudo().search(domain)
    return candidates.filtered(
        lambda transfer: picking.name in _origin_tokens(transfer.origin)
    )


def _get_next_transfers(picking):
    request_next = _get_next_request_transfers(picking)
    if request_next:
        return request_next
    return _get_next_origin_transfers(picking)


def _get_originals_from_returns():
    domain = [
        ("picking_type_code", "=", "internal"),
        ("state", "!=", "cancel"),
        ("move_ids_without_package.origin_returned_move_id", "!=", False),
    ]
    if ONLY_COMPANY_ID:
        domain.append(("company_id", "=", ONLY_COMPANY_ID))

    returns = env["stock.picking"].sudo().search(domain, order="id desc")
    original_to_returns = defaultdict(set)
    for ret in returns:
        originals = ret.move_ids_without_package.mapped("origin_returned_move_id.picking_id")
        for original in originals.filtered(lambda p: p and p.exists()):
            original_to_returns[original.id].add(ret.id)
    originals = env["stock.picking"].sudo().browse(list(original_to_returns.keys()))
    originals = originals.filtered(lambda p: p.picking_type_code == "internal")
    originals = originals.sorted(lambda p: p.id)
    if MAX_ORIGINALS:
        originals = originals[:MAX_ORIGINALS]
    return originals, original_to_returns


def run_backfill():
    originals, original_to_returns = _get_originals_from_returns()
    print("=" * 110)
    print("Backfill Cancel Next Transfers")
    print(
        "DRY_RUN={} | ONLY_COMPANY_ID={} | originals_found={}".format(
            DRY_RUN, ONLY_COMPANY_ID, len(originals)
        )
    )
    print("=" * 110)

    stats = defaultdict(int)
    details = []

    for original in originals:
        stats["processed_originals"] += 1
        try:
            next_transfers = _get_next_transfers(original)
            if not next_transfers:
                stats["no_next"] += 1
                continue

            done_next = next_transfers.filtered(lambda transfer: transfer.state == "done")
            pending_next = next_transfers.filtered(lambda transfer: transfer.state in PENDING_STATES)

            if done_next:
                stats["skipped_done_next"] += 1
                details.append(
                    {
                        "type": "SKIP_DONE_NEXT",
                        "original": original.name,
                        "original_id": original.id,
                        "returns": ",".join(env["stock.picking"].sudo().browse(list(original_to_returns[original.id])).mapped("name")),
                        "next_done": ",".join(done_next.mapped("name")),
                    }
                )
                continue

            if not pending_next:
                stats["no_pending_next"] += 1
                continue

            row = {
                "type": "CANCEL_NEXT",
                "original": original.name,
                "original_id": original.id,
                "returns": ",".join(env["stock.picking"].sudo().browse(list(original_to_returns[original.id])).mapped("name")),
                "next_pending": ",".join(["{}({})".format(p.name, p.state) for p in pending_next]),
            }

            if DRY_RUN:
                stats["would_cancel"] += len(pending_next)
                row["action"] = "dry_run"
            else:
                pending_next.with_context(tradeline_return_cancel_next=True).action_cancel()
                stats["canceled"] += len(pending_next)
                row["action"] = "canceled"

                if original.request_id and original.request_id.state != "cancel":
                    original.request_id.action_cancel()
                    stats["requests_canceled"] += 1
                if original.request_id:
                    # Keep source document chain consistent for request-linked flows.
                    original.request_id._tradeline_refresh_source_documents()

            details.append(row)
        except Exception as exc:
            stats["errors"] += 1
            details.append(
                {
                    "type": "ERROR",
                    "original": original.name,
                    "original_id": original.id,
                    "error": repr(exc),
                }
            )

    print("Summary:")
    for key in sorted(stats.keys()):
        print("  {}: {}".format(key, stats[key]))

    print("-" * 110)
    print("Details (first 120 rows):")
    for row in details[:120]:
        print(row)
    if len(details) > 120:
        print("... truncated {} rows ...".format(len(details) - 120))

    print("=" * 110)
    print("Done.")


run_backfill()

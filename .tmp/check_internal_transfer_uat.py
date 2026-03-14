"""
Read-only UAT checker for:
1) Case 1: Source Document chain normalization.
2) Case 2: Return cancels next transfer(s) for request-linked internal transfers.

Run from Odoo shell context where `env` is available:
    odoo-bin shell -d <db_name> -c <odoo_conf> < d:\\tradline\\.tmp\\check_internal_transfer_uat.py
"""

from collections import defaultdict


# -----------------------
# Optional filters
# -----------------------
REQUEST_IDS = []  # Example: [101, 102]
PICKING_IDS = []  # Example: [5567]
FROM_DATETIME = None  # Example: "2026-03-14 00:00:00"
MAX_PICKINGS = 1500
PRINT_LIMIT = 40


PENDING_STATES = {"draft", "waiting", "confirmed", "assigned", "partially_available"}


def _normalize_refs(refs):
    ordered = []
    seen = set()
    for ref in refs:
        ref = (ref or "").strip()
        if not ref or ref in seen:
            continue
        seen.add(ref)
        ordered.append(ref)
    return ",".join(ordered)


def _get_chain_transfers(picking):
    """Same logic used in custom code: previous + next by location link."""
    request_transfers = picking.request_id.transfer_ids.filtered(
        lambda t: t.picking_type_code == "internal" and t.request_id.id == picking.request_id.id
    )
    related = request_transfers.filtered(lambda t: t.id != picking.id)
    previous = related.filtered(lambda t: t.location_dest_id.id == picking.location_id.id)
    next_transfers = related.filtered(lambda t: t.location_id.id == picking.location_dest_id.id)
    linked = (previous | next_transfers).sorted(lambda t: t.id)
    return linked, next_transfers.sorted(lambda t: t.id)


def _find_return_pickings_from_original(original_picking, Picking):
    """Return pickings linked via origin_returned_move_id."""
    move_ids = original_picking.move_ids_without_package.ids
    if not move_ids:
        return Picking.browse()
    return Picking.search(
        [
            ("picking_type_code", "=", "internal"),
            ("move_ids_without_package.origin_returned_move_id", "in", move_ids),
        ]
    )


def _get_candidate_pickings(env):
    Picking = env["stock.picking"].sudo()
    Request = env["transfer.request"].sudo()

    if PICKING_IDS:
        return Picking.browse(PICKING_IDS).filtered(
            lambda p: p.request_id and p.picking_type_code == "internal"
        )

    if REQUEST_IDS:
        requests = Request.browse(REQUEST_IDS).exists()
        return requests.mapped("transfer_ids").filtered(
            lambda p: p.request_id and p.picking_type_code == "internal"
        )

    domain = [("request_id", "!=", False), ("picking_type_code", "=", "internal")]
    if FROM_DATETIME:
        domain.append(("create_date", ">=", FROM_DATETIME))
    return Picking.search(domain, limit=MAX_PICKINGS, order="id desc")


def _print_issues(title, rows, headers):
    print("\n" + "=" * 100)
    print(title)
    print("=" * 100)
    if not rows:
        print("PASS: no issues found")
        return
    print("FAIL: {} issue(s) found".format(len(rows)))
    print(" | ".join(headers))
    for row in rows[:PRINT_LIMIT]:
        print(" | ".join(str(x) for x in row))
    if len(rows) > PRINT_LIMIT:
        print("... truncated {} row(s) ...".format(len(rows) - PRINT_LIMIT))


def run():
    Picking = env["stock.picking"].sudo()
    pickings = _get_candidate_pickings(env)

    case1_issues = []
    case2_issues = []
    stats = defaultdict(int)

    for picking in pickings:
        stats["checked_pickings"] += 1

        # -----------------------
        # Case 1: Source Document
        # -----------------------
        linked, next_transfers = _get_chain_transfers(picking)
        expected_origin = _normalize_refs(linked.mapped("name"))
        actual_origin = (picking.origin or "").strip()
        if actual_origin != expected_origin:
            case1_issues.append(
                (
                    picking.request_id.name,
                    picking.name,
                    picking.state,
                    actual_origin,
                    expected_origin,
                )
            )
        else:
            stats["case1_pass"] += 1

        # -----------------------
        # Case 2: Return behavior
        # -----------------------
        return_pickings = _find_return_pickings_from_original(picking, Picking)
        if not return_pickings:
            continue

        stats["with_returns"] += 1
        done_next = next_transfers.filtered(lambda t: t.state == "done")
        pending_next = next_transfers.filtered(lambda t: t.state in PENDING_STATES)

        # Rule A: if next is done, return should have been blocked.
        if done_next:
            case2_issues.append(
                (
                    picking.request_id.name,
                    picking.name,
                    ",".join(return_pickings.mapped("name")),
                    "Return exists although next transfer is done",
                    ",".join(done_next.mapped("name")),
                )
            )
            continue

        # Rule B: if return exists and next transfers exist, pending next must be canceled.
        if next_transfers and pending_next:
            case2_issues.append(
                (
                    picking.request_id.name,
                    picking.name,
                    ",".join(return_pickings.mapped("name")),
                    "Pending next transfer not canceled",
                    ",".join(["{}({})".format(t.name, t.state) for t in pending_next]),
                )
            )
            continue

        # Rule C: if next transfers exist, request should be canceled.
        if next_transfers and picking.request_id.state != "cancel":
            case2_issues.append(
                (
                    picking.request_id.name,
                    picking.name,
                    ",".join(return_pickings.mapped("name")),
                    "Request not canceled after return flow",
                    "request_state={}".format(picking.request_id.state),
                )
            )
            continue

        stats["case2_pass"] += 1

    print("\n" + "#" * 100)
    print("INTERNAL TRANSFER UAT CHECK")
    print("#" * 100)
    print("Checked pickings: {}".format(stats["checked_pickings"]))
    print("Pickings with return(s): {}".format(stats["with_returns"]))
    print("Case1 passes: {}".format(stats["case1_pass"]))
    print("Case2 passes: {}".format(stats["case2_pass"]))

    _print_issues(
        "CASE 1 ISSUES (Source Document mismatch)",
        case1_issues,
        ["Request", "Picking", "State", "Actual Source Document", "Expected Source Document"],
    )
    _print_issues(
        "CASE 2 ISSUES (Return flow mismatch)",
        case2_issues,
        ["Request", "Original Picking", "Return Picking(s)", "Issue", "Details"],
    )

    print("\nDone.")


run()

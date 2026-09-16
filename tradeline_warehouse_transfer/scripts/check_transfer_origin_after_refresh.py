"""
Run inside Odoo shell to validate source document behavior after refresh.

Examples:
    odoo-bin shell -d <db_name> -c <odoo.conf> < tradeline_warehouse_transfer/scripts/check_transfer_origin_after_refresh.py

Optional variables before execution (edit in file or inject in shell globals):
    TRANSFER_NAME = "Mohan/INT/00254"
"""

TRANSFER_NAME = globals().get("TRANSFER_NAME", "Mohan/INT/00254")


def _split_refs(value):
    return [ref.strip() for ref in (value or "").split(",") if ref and ref.strip()]


def _print_transfer_snapshot(picking, title):
    refs = _split_refs(picking.origin)
    print(f"\n[{title}]")
    print(f"Transfer: {picking.name} (id={picking.id}, state={picking.state})")
    print(f"Request: {picking.request_id.name if picking.request_id else '-'}")
    print(f"From -> To: {picking.location_id.display_name} -> {picking.location_dest_id.display_name}")
    print(f"Origin raw: {picking.origin or '-'}")
    print(f"Origin refs ({len(refs)}): {refs or ['-']}")
    print(f"Computed chain refs: {picking._tradeline_get_chain_source_document_refs()}")
    if len(refs) > 1:
        print("Status: FAIL (origin still combined)")
    else:
        print("Status: PASS (origin is single or empty)")


def run():
    picking = env["stock.picking"].search([("name", "=", TRANSFER_NAME)], limit=1)
    if not picking:
        picking = env["stock.picking"].search([("name", "ilike", TRANSFER_NAME)], limit=1)
    if not picking:
        print(f"ERROR: transfer not found for '{TRANSFER_NAME}'")
        return

    _print_transfer_snapshot(picking, "BEFORE REFRESH")

    if picking.request_id:
        picking.request_id._tradeline_refresh_source_documents()
        env.cr.commit()
    else:
        print("\nNo request_id found, skipping refresh call.")

    picking.invalidate_recordset()
    picking = env["stock.picking"].browse(picking.id)
    _print_transfer_snapshot(picking, "AFTER REFRESH")

    if picking.request_id:
        chain = picking.request_id.transfer_ids.filtered(lambda t: t.picking_type_code == "internal").sorted(lambda t: t.id)
        print("\n[REQUEST INTERNAL TRANSFERS]")
        for transfer in chain:
            print(f"- {transfer.name} | state={transfer.state} | origin={transfer.origin or '-'}")


run()

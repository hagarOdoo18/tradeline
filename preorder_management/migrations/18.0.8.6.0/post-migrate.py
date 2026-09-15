def migrate(cr, version):
    # Payment reversals are financially material and must be initiated from the
    # explicit manager action on the selected pre-orders, never during upgrade.
    return

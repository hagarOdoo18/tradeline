def migrate(cr, version):
    # Preserve each existing update's location company when adding company rules.
    cr.execute("""
        UPDATE stock_multi_update AS adjustment
           SET company_id = location.company_id
          FROM stock_location location
         WHERE location.id = adjustment.location_id
           AND location.company_id IS NOT NULL
           AND adjustment.company_id IS DISTINCT FROM location.company_id
    """)

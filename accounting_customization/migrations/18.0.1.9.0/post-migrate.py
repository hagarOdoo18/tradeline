from psycopg2 import sql

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """Clear SRO taxes and refresh amounts left stale on locked orders."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    sro_lines = env['sale.order.line'].search([
        ('order_id.inv_type', '=', 'sro'),
    ])
    if not sro_lines:
        return

    tax_field = env['sale.order.line']._fields['tax_id']
    cr.execute(
        sql.SQL("""
            DELETE FROM {relation} AS rel
            USING sale_order_line AS line, sale_order AS sale
            WHERE rel.{line_column} = line.id
              AND sale.id = line.order_id
              AND sale.inv_type = %s
        """).format(
            relation=sql.Identifier(tax_field.relation),
            line_column=sql.Identifier(tax_field.column1),
        ),
        ('sro',),
    )

    sro_lines.invalidate_recordset(['tax_id'])
    sro_lines._compute_amount()
    sro_lines.flush_recordset(['price_subtotal', 'price_tax', 'price_total'])

    sro_orders = sro_lines.order_id
    sro_orders._compute_amounts()
    sro_orders.flush_recordset(['amount_untaxed', 'amount_tax', 'amount_total'])

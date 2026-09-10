from psycopg2 import sql

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """Remove taxes left on SRO order lines by earlier module versions."""
    env = api.Environment(cr, SUPERUSER_ID, {})
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

from odoo import Command, SUPERUSER_ID, api


def migrate(cr, version):
    """Remove taxes left on SRO order lines by earlier module versions."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    sro_lines = env['sale.order.line'].search([
        ('order_id.inv_type', '=', 'sro'),
        ('tax_id', '!=', False),
    ])
    if sro_lines:
        sro_lines.write({'tax_id': [Command.clear()]})

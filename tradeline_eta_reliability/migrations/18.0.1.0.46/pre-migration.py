"""Prepare only our child XML before the parent summary panel is removed."""
import json

from lxml import etree
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    # Own model methods are not registered at pre-migration time, so an ORM view
    # write would validate the whole old form against an incomplete registry.
    # Normal module XML loading validates these views after models are ready.
    cr.execute("""SELECT v.id, d.name, v.arch_db
        FROM ir_ui_view v JOIN ir_model_data d
          ON d.model = 'ir.ui.view' AND d.res_id = v.id
        WHERE d.module = %s AND d.name IN %s""", [
        'tradeline_eta_reliability',
        ('view_eta_vat_review_invoice', 'view_move_form_branch_eta_visibility')])
    for view_id, name, translations in cr.fetchall():
        replacement = {}
        for language, arch in translations.items():
            if name == 'view_eta_vat_review_invoice':
                replacement[language] = """<data>
                    <xpath expr="//field[@name='eta_staff_state']" position="attributes">
                        <attribute name="invisible">1</attribute>
                    </xpath>
                </data>"""
            else:
                root = etree.fromstring(arch)
                obsolete = "//div[@role='status'][.//field[@name='eta_staff_reason']]"
                for node in root.xpath('//xpath'):
                    if node.get('expr') == obsolete:
                        node.getparent().remove(node)
                replacement[language] = etree.tostring(root, encoding='unicode')
        cr.execute('UPDATE ir_ui_view SET arch_db = %s::jsonb WHERE id = %s',
                   [json.dumps(replacement), view_id])
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['ir.ui.view'].invalidate_model(['arch_db'])
    env.registry.clear_cache()

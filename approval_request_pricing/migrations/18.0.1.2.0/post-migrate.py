from odoo import SUPERUSER_ID, api


def _migrate_options(env, rows, value_index, model_name, field_name):
    option_model = env[model_name].with_context(active_test=False)
    option_ids = {}
    for request_id, *values in rows:
        value = values[value_index]
        if not value:
            continue
        option = option_ids.get(value)
        if not option:
            option = option_model.search([("name", "=", value)], limit=1)
            if not option:
                option = option_model.create({"name": value})
            option_ids[value] = option
        env["approval.request"].browse(request_id).write({field_name: option.id})


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    cr.execute(
        """
        SELECT id, payment_terms, method_type
          FROM approval_request
         WHERE COALESCE(payment_terms, '') != ''
            OR COALESCE(method_type, '') != ''
        """
    )
    rows = cr.fetchall()
    _migrate_options(
        env,
        rows,
        0,
        "approval.payment.term.option",
        "payment_term_option_id",
    )
    _migrate_options(
        env,
        rows,
        1,
        "approval.method.type.option",
        "method_type_option_id",
    )

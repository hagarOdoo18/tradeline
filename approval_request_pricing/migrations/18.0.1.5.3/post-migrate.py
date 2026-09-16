from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """Remove memberships inherited from the former Settings-admin grant."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    approvals_group = env.ref(
        "approval_request_pricing.group_approvals", raise_if_not_found=False
    )
    system_group = env.ref("base.group_system", raise_if_not_found=False)
    if not approvals_group or not system_group:
        return

    inherited_administrators = approvals_group.users & system_group.users
    if inherited_administrators:
        approvals_group.write(
            {"users": [(3, user_id) for user_id in inherited_administrators.ids]}
        )

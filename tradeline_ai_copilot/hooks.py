def post_init_hook(env):
    env["ai.copilot.settings"].sudo().get_singleton()
    env["ai.copilot.service"].sudo().refresh_allowed_models()


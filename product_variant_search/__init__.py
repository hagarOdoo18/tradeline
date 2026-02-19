from . import models
def _compute_existing_variant_names(env):
    """Recompute variant_name for all existing products on install."""
    field = env['product.product']._fields['variant_name']
    products = env['product.product'].search([])
    env.add_to_compute(field, products)

/** @odoo-module */
import { onMounted, onWillUnmount } from '@odoo/owl';
import { ConnectionLostError, ConnectionAbortedError } from '@web/core/network/rpc';
import { _t } from '@web/core/l10n/translation';
import { patch } from '@web/core/utils/patch';
import { handleRPCError } from "@point_of_sale/app/errors/error_handlers";
import { ProductScreen } from '@point_of_sale/app/screens/product_screen/product_screen';

patch(ProductScreen.prototype, {
  setup() {
    super.setup(...arguments);

    onMounted(() => {
      if (this.pos.config.update_stock_quantities === 'real') {
        const refreshRate = this.pos.config.stock_quantities_refresh_rate;
        const refreshRateMS = refreshRate && refreshRate * 60000;
        this._runUpdateProductQtyTimer(refreshRateMS);
        this.loadLatestProductQtyFromDB();
      }
    });

    onWillUnmount(() => {
      if (this.pos.config.update_stock_quantities === 'real') {
        clearInterval(this.updateProductQtyTimer);
      }
    });
  },

  _runUpdateProductQtyTimer(time = 180000) {
    this.updateProductQtyTimer = setInterval(() => {
      this.loadLatestProductQtyFromDB();
    }, time);
  },

  async loadLatestProductQtyFromDB() {
    try {
      const allVariants = this.pos.models['product.product'].getAll();
      if (!allVariants.length) return;

      // ✅ Build lookup maps ONCE — O(n) instead of repeated .get() calls
      const variantMap = new Map(allVariants.map((v) => [v.id, v]));

      // ✅ Build template → variants map ONCE upfront
      const tmplVariantsMap = new Map();
      for (const variant of allVariants) {
        const tmplId =
          typeof variant.product_tmpl_id === 'object'
            ? variant.product_tmpl_id[0]
            : variant.product_tmpl_id;
        if (tmplId) {
          if (!tmplVariantsMap.has(tmplId)) tmplVariantsMap.set(tmplId, []);
          tmplVariantsMap.get(tmplId).push(variant);
        }
      }

      const variantIds = [...variantMap.keys()];

      let kwargs = {};
      if (this.pos.config.stock_warehouse === 'current') {
        const locationId = this.pos.config.picking_type_id?.default_location_src_id?.id;
        if (locationId) kwargs.context = { location: locationId };
      }

      // ✅ Fetch only required fields
      const updatedVariants = await this.pos.data.searchRead(
        'product.product',
        [['id', 'in', variantIds]],
        ['qty_available', 'virtual_available', 'product_tmpl_id'],
        kwargs
      );

      if (!updatedVariants?.length) return;

      // ✅ Track which templates need recalculation (avoid duplicates)
      const dirtyTemplateIds = new Set();

      // Step 1: Update all variants first — O(n)
      for (const variantData of updatedVariants) {
        const variant = variantMap.get(variantData.id);
        if (!variant) continue;

        variant.qty_available = variantData.qty_available;
        variant.virtual_available = variantData.virtual_available;

        const tmplId =
          typeof variantData.product_tmpl_id === 'object'
            ? variantData.product_tmpl_id[0]
            : variantData.product_tmpl_id;

        if (tmplId) dirtyTemplateIds.add(tmplId);
      }

      // Step 2: Recalculate only affected templates ONCE each — O(m)
      for (const tmplId of dirtyTemplateIds) {
        const template = this.pos.models['product.template']?.get(tmplId);
        if (!template) continue;

        const variants = tmplVariantsMap.get(tmplId) || [];
        template.qty_available = variants.reduce((sum, v) => sum + (v.qty_available || 0), 0);
        template.virtual_available = variants.reduce((sum, v) => sum + (v.virtual_available || 0), 0);
      }

    } catch (error) {
      if (error instanceof ConnectionLostError || error instanceof ConnectionAbortedError) {
        return this.popup.add(handleRPCError, {
          title: _t('Network Error'),
          body: _t('Product stock could not be refreshed. Please check your network connection.'),
        });
      }
      throw error;
    }
  },
});
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
      // getAll() returns an Array in Odoo 18 — not an object
      const allVariants = this.pos.models['product.product'].getAll();
      if (!allVariants.length) return;

      // O(1) lookup map by id
      const variantMap = new Map(allVariants.map((v) => [v.id, v]));

      // Pre-build template → variants map once (avoids O(n²) later)
      const tmplVariantsMap = new Map();
      for (const variant of allVariants) {
        const tmplId = variant.product_tmpl_id?.id ?? variant.product_tmpl_id;
        if (tmplId) {
          if (!tmplVariantsMap.has(tmplId)) tmplVariantsMap.set(tmplId, []);
          tmplVariantsMap.get(tmplId).push(variant);
        }
      }

      // Build context: inject location when config is 'current' warehouse
      const context = {};
      if (this.pos.config.stock_warehouse === 'current') {
        const locationId =
          this.pos.config.picking_type_id?.default_location_src_id?.id;
        if (locationId) context.location = locationId;
      }

      // Fetch updated quantities from server
      const updatedVariants = await this.env.services.orm.searchRead(
        'product.product',
        [['id', 'in', [...variantMap.keys()]]],
        ['qty_available', 'virtual_available', 'product_tmpl_id'],
        { context }
      );

      if (!updatedVariants?.length) return;

      const dirtyTemplateIds = new Set();

      // Step 1: Update each variant using Object.assign for OWL reactivity
      for (const data of updatedVariants) {
        const variant = variantMap.get(data.id);
        if (!variant) continue;

        Object.assign(variant, {
          qty_available: data.qty_available,
          virtual_available: data.virtual_available,
        });

        // product_tmpl_id from orm.searchRead is a [id, name] tuple
        const tmplId = Array.isArray(data.product_tmpl_id)
          ? data.product_tmpl_id[0]
          : data.product_tmpl_id?.id ?? data.product_tmpl_id;

        if (tmplId) dirtyTemplateIds.add(tmplId);
      }

      // Step 2: Re-aggregate qty on affected templates (each template only once)
      for (const tmplId of dirtyTemplateIds) {
        const template = this.pos.models['product.template']?.get(tmplId);
        if (!template) continue;

        const variants = tmplVariantsMap.get(tmplId) || [];
        Object.assign(template, {
          qty_available: variants.reduce((s, v) => s + (v.qty_available || 0), 0),
          virtual_available: variants.reduce((s, v) => s + (v.virtual_available || 0), 0),
        });
      }

    } catch (error) {
      if (
        error instanceof ConnectionLostError ||
        error instanceof ConnectionAbortedError
      ) {
        return this.popup.add(handleRPCError, {
          title: _t('Network Error'),
          body: _t(
            'Product stock could not be refreshed. Please check your network connection.'
          ),
        });
      }
      console.error('[loadLatestProductQtyFromDB]', error);
      throw error;
    }
  },
});

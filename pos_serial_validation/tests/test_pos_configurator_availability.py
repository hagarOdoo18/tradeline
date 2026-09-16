from odoo.tests.common import TransactionCase


class TestPosConfiguratorAvailability(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ProductTemplate = cls.env["product.template"]
        cls.ProductAttribute = cls.env["product.attribute"]
        cls.ProductAttributeValue = cls.env["product.attribute.value"]
        cls.StockLot = cls.env["stock.lot"]
        cls.StockQuant = cls.env["stock.quant"]
        cls.StockLocation = cls.env["stock.location"]
        cls.PosConfig = cls.env["pos.config"]

        cls.company = cls.env.company
        cls.stock_location_root = cls.env.ref("stock.stock_location_locations")
        cls.customer_location = cls.env.ref("stock.stock_location_customers")
        cls.outgoing_picking_type = cls.env.ref("stock.picking_type_out")

        cls.pos_location = cls.StockLocation.create(
            {
                "name": "POS Configurator Source",
                "usage": "internal",
                "location_id": cls.stock_location_root.id,
                "company_id": cls.company.id,
            }
        )
        cls.pos_picking_type = cls.outgoing_picking_type.copy(
            {
                "name": "POS Configurator Picking",
                "default_location_src_id": cls.pos_location.id,
                "default_location_dest_id": cls.customer_location.id,
                "sequence_code": "TPOSCFG",
            }
        )

        cls.pos_journal = cls.env["account.journal"].search(
            [("company_id", "=", cls.company.id), ("type", "in", ("cash", "bank"))],
            limit=1,
        ) or cls.env["account.journal"].search([("company_id", "=", cls.company.id)], limit=1)

        cls.pos_config = cls.PosConfig.create(
            {
                "name": "POS Configurator",
                "journal_id": cls.pos_journal.id if cls.pos_journal else False,
                "picking_type_id": cls.pos_picking_type.id,
            }
        )

        cls.color_attribute, cls.color_values = cls._create_attribute(
            "Color", ["Black", "Orange", "Purple"]
        )
        cls.vendor_attribute, cls.vendor_values = cls._create_attribute(
            "Vendor", ["ABM", "Ram"], is_vendor=True
        )

    @classmethod
    def _create_attribute(cls, name, value_names, is_vendor=False):
        attr_vals = {
            "name": name,
            "create_variant": "always",
            "display_type": "radio",
        }
        if "is_vendor" in cls.ProductAttribute._fields:
            attr_vals["is_vendor"] = is_vendor

        attribute = cls.ProductAttribute.create(attr_vals)
        values = cls.ProductAttributeValue.create(
            [{"name": value_name, "attribute_id": attribute.id} for value_name in value_names]
        )
        value_by_name = {value.name: value for value in values}
        return attribute, value_by_name

    @classmethod
    def _create_template(cls, name, tracking="none"):
        return cls.ProductTemplate.create(
            {
                "name": name,
                "sale_ok": True,
                "available_in_pos": True,
                "is_storable": True,
                "tracking": tracking,
                "attribute_line_ids": [
                    (
                        0,
                        0,
                        {
                            "attribute_id": cls.color_attribute.id,
                            "value_ids": [(6, 0, [value.id for value in cls.color_values.values()])],
                        },
                    ),
                    (
                        0,
                        0,
                        {
                            "attribute_id": cls.vendor_attribute.id,
                            "value_ids": [(6, 0, [value.id for value in cls.vendor_values.values()])],
                        },
                    ),
                ],
            }
        )

    @staticmethod
    def _ptav_by_name(attribute_line):
        return {
            ptav.product_attribute_value_id.name: ptav for ptav in attribute_line.product_template_value_ids
        }

    @staticmethod
    def _map_get(mapping, key, default=None):
        if not isinstance(mapping, dict):
            return default
        return mapping.get(key, mapping.get(str(key), default))

    def _variant_for(self, template, ptav_a, ptav_b):
        target_ids = {ptav_a.id, ptav_b.id}
        variants = template.product_variant_ids.filtered(
            lambda variant: set(variant.product_template_variant_value_ids.ids) == target_ids
        )
        self.assertEqual(len(variants), 1)
        return variants[0]

    def test_non_serial_filters_values_and_auto_selects_vendor(self):
        template = self._create_template("POS Configurator Non Serial")

        color_line = template.attribute_line_ids.filtered(
            lambda line: line.attribute_id.id == self.color_attribute.id
        )
        vendor_line = template.attribute_line_ids.filtered(
            lambda line: line.attribute_id.id == self.vendor_attribute.id
        )

        color_ptav = self._ptav_by_name(color_line)
        vendor_ptav = self._ptav_by_name(vendor_line)

        self.StockQuant._update_available_quantity(
            self._variant_for(template, color_ptav["Black"], vendor_ptav["ABM"]),
            self.pos_location,
            1.0,
        )
        self.StockQuant._update_available_quantity(
            self._variant_for(template, color_ptav["Black"], vendor_ptav["Ram"]),
            self.pos_location,
            5.0,
        )
        self.StockQuant._update_available_quantity(
            self._variant_for(template, color_ptav["Orange"], vendor_ptav["ABM"]),
            self.pos_location,
            3.0,
        )

        payload = self.ProductTemplate.get_pos_configurator_availability(template.id, self.pos_config.id)

        self.assertIn(vendor_line.id, payload["hide_line_ids"])
        self.assertFalse(payload["is_blocked"])
        self.assertEqual(payload["stock_decision"], "ok")
        self.assertEqual(payload["consistency_status"], "ok")

        allowed_color_ids = set(self._map_get(payload["allowed_value_ids_by_line"], color_line.id, []))
        self.assertEqual(allowed_color_ids, {color_ptav["Black"].id, color_ptav["Orange"].id})
        self.assertNotIn(color_ptav["Purple"].id, allowed_color_ids)

        vendor_by_value = payload["vendor_value_by_value_id"]
        self.assertEqual(self._map_get(vendor_by_value, color_ptav["Black"].id), vendor_ptav["Ram"].id)
        self.assertEqual(self._map_get(vendor_by_value, color_ptav["Orange"].id), vendor_ptav["ABM"].id)
        self.assertEqual(payload["default_vendor_value_id"], vendor_ptav["Ram"].id)

    def test_tracked_template_hides_vendor_and_sets_default_combo(self):
        template = self._create_template("POS Configurator Serial", tracking="serial")
        color_line = template.attribute_line_ids.filtered(
            lambda line: line.attribute_id.id == self.color_attribute.id
        )
        vendor_line = template.attribute_line_ids.filtered(
            lambda line: line.attribute_id.id == self.vendor_attribute.id
        )

        color_ptav = self._ptav_by_name(color_line)
        vendor_ptav = self._ptav_by_name(vendor_line)

        black_abm_variant = self._variant_for(template, color_ptav["Black"], vendor_ptav["ABM"])
        black_abm_lot = self.StockLot.create(
            {
                "name": "CFG-SERIAL-BLACK-ABM",
                "product_id": black_abm_variant.id,
                "company_id": self.company.id,
                "location_id": self.pos_location.id,
            }
        )
        self.StockQuant._update_available_quantity(
            black_abm_variant,
            self.pos_location,
            1.0,
            lot_id=black_abm_lot,
        )

        payload = self.ProductTemplate.get_pos_configurator_availability(template.id, self.pos_config.id)

        self.assertIn(vendor_line.id, payload["hide_line_ids"])
        self.assertTrue(payload["is_tracked_product"])
        self.assertEqual(
            self._map_get(payload["allowed_value_ids_by_line"], color_line.id),
            [color_ptav["Black"].id],
        )
        self.assertEqual(
            self._map_get(payload["allowed_value_ids_by_line"], vendor_line.id),
            [vendor_ptav["ABM"].id],
        )
        self.assertEqual(
            self._map_get(payload["variant_value_by_value_id"], color_ptav["Black"].id),
            color_ptav["Black"].id,
        )
        self.assertEqual(
            self._map_get(payload["vendor_value_by_value_id"], color_ptav["Black"].id),
            vendor_ptav["ABM"].id,
        )
        self.assertEqual(payload["default_vendor_value_id"], vendor_ptav["ABM"].id)
        self.assertEqual(
            set(payload["default_attribute_value_ids"]),
            {color_ptav["Black"].id, vendor_ptav["ABM"].id},
        )
        self.assertFalse(payload["is_blocked"])

    def test_inactive_capacity_value_maps_to_active_display_value(self):
        capacity_attribute, capacity_values = self._create_attribute(
            "Capacity Mapping",
            ["128", "128GB", "256GB"],
        )
        template = self.ProductTemplate.create(
            {
                "name": "POS Configurator Inactive Capacity Mapping",
                "sale_ok": True,
                "available_in_pos": True,
                "is_storable": True,
                "tracking": "serial",
                "attribute_line_ids": [
                    (
                        0,
                        0,
                        {
                            "attribute_id": self.color_attribute.id,
                            "value_ids": [(6, 0, [self.color_values["Black"].id])],
                        },
                    ),
                    (
                        0,
                        0,
                        {
                            "attribute_id": capacity_attribute.id,
                            "value_ids": [
                                (
                                    6,
                                    0,
                                    [
                                        capacity_values["128"].id,
                                        capacity_values["128GB"].id,
                                        capacity_values["256GB"].id,
                                    ],
                                )
                            ],
                        },
                    ),
                ],
            }
        )

        color_line = template.attribute_line_ids.filtered(
            lambda line: line.attribute_id.id == self.color_attribute.id
        )
        capacity_line = template.attribute_line_ids.filtered(
            lambda line: line.attribute_id.id == capacity_attribute.id
        )

        color_ptav = self._ptav_by_name(color_line)
        capacity_ptav = self._ptav_by_name(capacity_line)

        if "ptav_active" in self.env["product.template.attribute.value"]._fields:
            capacity_ptav["128"].write({"ptav_active": False})

        variant = self._variant_for(template, color_ptav["Black"], capacity_ptav["128"])
        lot = self.StockLot.create(
            {
                "name": "CFG-SERIAL-CAP-MAP-001",
                "product_id": variant.id,
                "company_id": self.company.id,
                "location_id": self.pos_location.id,
            }
        )
        self.StockQuant._update_available_quantity(
            variant,
            self.pos_location,
            1.0,
            lot_id=lot,
        )

        payload = self.ProductTemplate.get_pos_configurator_availability(template.id, self.pos_config.id)

        self.assertFalse(payload["is_blocked"])
        self.assertIn(
            capacity_ptav["128GB"].id,
            self._map_get(payload["allowed_value_ids_by_line"], capacity_line.id, []),
        )
        self.assertEqual(
            self._map_get(payload["variant_value_by_value_id"], capacity_ptav["128GB"].id),
            capacity_ptav["128"].id,
        )
        self.assertIn(capacity_ptav["128GB"].id, payload["default_attribute_value_ids"])
        self.assertIn(capacity_ptav["128"].id, payload["default_variant_attribute_value_ids"])

    def test_blocked_when_all_variant_stock_is_zero(self):
        template = self._create_template("POS Configurator Zero Stock")

        color_line = template.attribute_line_ids.filtered(
            lambda line: line.attribute_id.id == self.color_attribute.id
        )
        vendor_line = template.attribute_line_ids.filtered(
            lambda line: line.attribute_id.id == self.vendor_attribute.id
        )

        payload = self.ProductTemplate.get_pos_configurator_availability(template.id, self.pos_config.id)

        self.assertTrue(payload["is_blocked"])
        self.assertTrue(payload["message"])
        self.assertEqual(payload["stock_decision"], "true_oos")
        self.assertIn(vendor_line.id, payload["hide_line_ids"])
        self.assertEqual(self._map_get(payload["allowed_value_ids_by_line"], color_line.id), [])

    def test_vendor_only_variant_auto_add_default_non_serial(self):
        template = self.ProductTemplate.create(
            {
                "name": "POS Configurator Vendor Only",
                "sale_ok": True,
                "available_in_pos": True,
                "is_storable": True,
                "tracking": "none",
                "attribute_line_ids": [
                    (
                        0,
                        0,
                        {
                            "attribute_id": self.vendor_attribute.id,
                            "value_ids": [(6, 0, [value.id for value in self.vendor_values.values()])],
                        },
                    ),
                ],
            }
        )
        vendor_line = template.attribute_line_ids
        vendor_ptav = self._ptav_by_name(vendor_line)
        abm_variant = template.product_variant_ids.filtered(
            lambda variant: set(variant.product_template_variant_value_ids.ids)
            == {vendor_ptav["ABM"].id}
        )
        self.assertEqual(len(abm_variant), 1)
        self.StockQuant._update_available_quantity(
            abm_variant[0],
            self.pos_location,
            2.0,
        )

        payload = self.ProductTemplate.get_pos_configurator_availability(template.id, self.pos_config.id)
        self.assertFalse(payload["is_blocked"])
        self.assertEqual(payload["stock_decision"], "ok")
        self.assertTrue(payload["auto_add_default"])

    def test_inconsistent_warning_when_context_missing(self):
        payload = self.ProductTemplate.get_pos_configurator_availability(False, False)
        self.assertFalse(payload["is_blocked"])
        self.assertEqual(payload["stock_decision"], "inconsistent")
        self.assertEqual(payload["consistency_status"], "inconsistent")
        self.assertTrue(payload["warning_message"])

    def test_child_location_stock_is_counted_for_availability(self):
        template = self._create_template("POS Configurator Child Location")
        color_line = template.attribute_line_ids.filtered(
            lambda line: line.attribute_id.id == self.color_attribute.id
        )
        vendor_line = template.attribute_line_ids.filtered(
            lambda line: line.attribute_id.id == self.vendor_attribute.id
        )
        color_ptav = self._ptav_by_name(color_line)
        vendor_ptav = self._ptav_by_name(vendor_line)

        child_location = self.StockLocation.create(
            {
                "name": "POS Configurator Child",
                "usage": "internal",
                "location_id": self.pos_location.id,
                "company_id": self.company.id,
            }
        )

        self.StockQuant._update_available_quantity(
            self._variant_for(template, color_ptav["Black"], vendor_ptav["ABM"]),
            child_location,
            2.0,
        )

        payload = self.ProductTemplate.get_pos_configurator_availability(template.id, self.pos_config.id)
        allowed_color_ids = set(self._map_get(payload["allowed_value_ids_by_line"], color_line.id, []))
        self.assertIn(color_ptav["Black"].id, allowed_color_ids)
        self.assertEqual(payload["stock_decision"], "ok")

    def test_public_qty_helper_returns_child_location_quantities(self):
        template = self._create_template("POS Configurator Qty Helper")
        color_line = template.attribute_line_ids.filtered(
            lambda line: line.attribute_id.id == self.color_attribute.id
        )
        vendor_line = template.attribute_line_ids.filtered(
            lambda line: line.attribute_id.id == self.vendor_attribute.id
        )
        color_ptav = self._ptav_by_name(color_line)
        vendor_ptav = self._ptav_by_name(vendor_line)

        child_location = self.StockLocation.create(
            {
                "name": "POS Configurator Qty Child",
                "usage": "internal",
                "location_id": self.pos_location.id,
                "company_id": self.company.id,
            }
        )
        variant = self._variant_for(template, color_ptav["Orange"], vendor_ptav["Ram"])
        self.StockQuant._update_available_quantity(variant, child_location, 4.0)

        qty_by_product = self.ProductTemplate.get_pos_available_qty_by_products(
            self.pos_config.id,
            [variant.id],
        )
        self.assertEqual(self._map_get(qty_by_product, variant.id, 0.0), 4.0)

    def test_insufficient_requested_qty_returns_specific_message(self):
        template = self._create_template("POS Configurator Insufficient Qty")
        color_line = template.attribute_line_ids.filtered(
            lambda line: line.attribute_id.id == self.color_attribute.id
        )
        vendor_line = template.attribute_line_ids.filtered(
            lambda line: line.attribute_id.id == self.vendor_attribute.id
        )
        color_ptav = self._ptav_by_name(color_line)
        vendor_ptav = self._ptav_by_name(vendor_line)

        self.StockQuant._update_available_quantity(
            self._variant_for(template, color_ptav["Black"], vendor_ptav["ABM"]),
            self.pos_location,
            2.0,
        )

        payload = self.ProductTemplate.get_pos_configurator_availability(
            template.id,
            self.pos_config.id,
            qty=3,
        )
        self.assertTrue(payload["is_blocked"])
        self.assertEqual(payload["stock_decision"], "true_oos")
        self.assertIn("Requested quantity", payload["message"])

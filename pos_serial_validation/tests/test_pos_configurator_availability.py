from odoo.tests.common import TransactionCase


class TestPosConfiguratorAvailability(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ProductTemplate = cls.env["product.template"]
        cls.ProductAttribute = cls.env["product.attribute"]
        cls.ProductAttributeValue = cls.env["product.attribute.value"]
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

        allowed_color_ids = set(payload["allowed_value_ids_by_line"][color_line.id])
        self.assertEqual(allowed_color_ids, {color_ptav["Black"].id, color_ptav["Orange"].id})
        self.assertNotIn(color_ptav["Purple"].id, allowed_color_ids)

        vendor_by_value = payload["vendor_value_by_value_id"]
        self.assertEqual(vendor_by_value[color_ptav["Black"].id], vendor_ptav["Ram"].id)
        self.assertEqual(vendor_by_value[color_ptav["Orange"].id], vendor_ptav["ABM"].id)
        self.assertEqual(payload["default_vendor_value_id"], vendor_ptav["Ram"].id)

    def test_tracked_template_skips_vendor_hide_behavior(self):
        template = self._create_template("POS Configurator Serial", tracking="serial")

        payload = self.ProductTemplate.get_pos_configurator_availability(template.id, self.pos_config.id)

        self.assertEqual(payload["hide_line_ids"], [])
        self.assertEqual(payload["allowed_value_ids_by_line"], {})
        self.assertEqual(payload["vendor_value_by_value_id"], {})
        self.assertFalse(payload["default_vendor_value_id"])
        self.assertFalse(payload["is_blocked"])

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
        self.assertIn(vendor_line.id, payload["hide_line_ids"])
        self.assertEqual(payload["allowed_value_ids_by_line"][color_line.id], [])

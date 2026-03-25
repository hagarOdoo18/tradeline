from odoo.tests.common import TransactionCase


class TestStockLotSearch(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.StockLot = cls.env["stock.lot"]
        cls.StockQuant = cls.env["stock.quant"]
        cls.ProductProduct = cls.env["product.product"]
        cls.PosConfig = cls.env["pos.config"]
        cls.StockLocation = cls.env["stock.location"]

        cls.company = cls.env.company
        cls.stock_location = cls.env.ref("stock.stock_location_stock")
        cls.stock_location_root = cls.env.ref("stock.stock_location_locations")
        cls.customer_location = cls.env.ref("stock.stock_location_customers")
        cls.outgoing_picking_type = cls.env.ref("stock.picking_type_out")
        cls.pos_journal = cls.env["account.journal"].search(
            [("company_id", "=", cls.company.id), ("type", "in", ("cash", "bank"))],
            limit=1,
        ) or cls.env["account.journal"].search([("company_id", "=", cls.company.id)], limit=1)

        cls.alt_location = cls.StockLocation.create(
            {
                "name": "POS Alt Source",
                "usage": "internal",
                "location_id": cls.stock_location_root.id,
                "company_id": cls.company.id,
            }
        )
        cls.alt_picking_type = cls.outgoing_picking_type.copy(
            {
                "name": "POS Alt Picking Type",
                "default_location_src_id": cls.alt_location.id,
                "default_location_dest_id": cls.customer_location.id,
                "sequence_code": "TPOSALT",
            }
        )

        cls.pos_config = cls.PosConfig.create(
            {
                "name": "POS Search Main",
                "journal_id": cls.pos_journal.id if cls.pos_journal else False,
                "picking_type_id": cls.outgoing_picking_type.id,
                "enable_product_bar_lot_serial_search": True,
            }
        )
        cls.alt_pos_config = cls.PosConfig.create(
            {
                "name": "POS Search Alt",
                "journal_id": cls.pos_journal.id if cls.pos_journal else False,
                "picking_type_id": cls.alt_picking_type.id,
                "enable_product_bar_lot_serial_search": True,
            }
        )

        cls.serial_product = cls._create_product(
            "POS Serial Product",
            "SERIAL-PRODUCT-001",
            tracking="serial",
        )
        cls.lot_product = cls._create_product(
            "POS Lot Product",
            "LOT-PRODUCT-001",
            tracking="lot",
        )
        cls.duplicate_product_a = cls._create_product(
            "Duplicate Lot Product A",
            "DUPLICATE-PRODUCT-A",
            tracking="serial",
        )
        cls.duplicate_product_b = cls._create_product(
            "Duplicate Lot Product B",
            "DUPLICATE-PRODUCT-B",
            tracking="serial",
        )
        cls.company_isolated_product = cls._create_product(
            "Company Isolated Product",
            "COMPANY-ISOLATED-001",
            tracking="serial",
        )

        cls.exact_serial = cls._create_lot("SN-EXACT-001", cls.serial_product, cls.stock_location)
        cls.partial_serial = cls._create_lot("SN-PARTIAL-123", cls.serial_product, cls.stock_location)
        cls.available_lot = cls._create_lot("LOT-SEARCH-001", cls.lot_product, cls.stock_location)
        cls.sold_serial = cls._create_lot(
            "SN-SOLD-001",
            cls.serial_product,
            cls.stock_location,
            serial_status="sold",
        )
        cls.returned_serial = cls._create_lot(
            "SN-RETURNED-001",
            cls.serial_product,
            cls.stock_location,
            serial_status="returned",
        )
        cls.zero_qty_lot = cls._create_lot("SN-ZERO-001", cls.serial_product, cls.stock_location)
        cls.duplicate_lot_a = cls._create_lot("DUP-LOT-001", cls.duplicate_product_a, cls.stock_location)
        cls.duplicate_lot_b = cls._create_lot("DUP-LOT-001", cls.duplicate_product_b, cls.stock_location)

        cls.StockQuant._update_available_quantity(cls.serial_product, cls.stock_location, 1.0, lot_id=cls.exact_serial)
        cls.StockQuant._update_available_quantity(cls.serial_product, cls.stock_location, 1.0, lot_id=cls.partial_serial)
        cls.StockQuant._update_available_quantity(cls.lot_product, cls.stock_location, 5.0, lot_id=cls.available_lot)
        cls.StockQuant._update_available_quantity(cls.serial_product, cls.stock_location, 1.0, lot_id=cls.sold_serial)
        cls.StockQuant._update_available_quantity(cls.serial_product, cls.stock_location, 1.0, lot_id=cls.returned_serial)
        cls.StockQuant._update_available_quantity(
            cls.duplicate_product_a, cls.stock_location, 1.0, lot_id=cls.duplicate_lot_a
        )
        cls.StockQuant._update_available_quantity(
            cls.duplicate_product_b, cls.stock_location, 1.0, lot_id=cls.duplicate_lot_b
        )

        cls.second_company = cls.env["res.company"].create({"name": "POS Search Company 2"})
        cls.second_company_location = cls.StockLocation.create(
            {
                "name": "Company 2 Stock",
                "usage": "internal",
                "location_id": cls.stock_location_root.id,
                "company_id": cls.second_company.id,
            }
        )
        cls.company_isolated_lot = cls.StockLot.create(
            {
                "name": "SN-COMPANY-002",
                "product_id": cls.company_isolated_product.id,
                "company_id": cls.second_company.id,
                "location_id": cls.second_company_location.id,
            }
        )
        cls.StockQuant.with_company(cls.second_company)._update_available_quantity(
            cls.company_isolated_product,
            cls.second_company_location,
            1.0,
            lot_id=cls.company_isolated_lot,
        )

    @classmethod
    def _create_product(cls, name, barcode, tracking):
        return cls.ProductProduct.create(
            {
                "name": name,
                "barcode": barcode,
                "available_in_pos": True,
                "sale_ok": True,
                "tracking": tracking,
                "is_storable": True,
            }
        )

    @classmethod
    def _create_lot(cls, name, product, location, serial_status="available"):
        return cls.StockLot.create(
            {
                "name": name,
                "product_id": product.id,
                "company_id": cls.company.id,
                "location_id": location.id,
                "serial_status": serial_status,
            }
        )

    def _search(self, query, pos_config=None):
        result = self.StockLot.search_pos_products(query, pos_config_id=(pos_config or self.pos_config).id)
        return result["products"]

    def test_search_pos_products_exact_serial(self):
        result = self._search("SN-EXACT-001")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["product_id"], self.serial_product.id)
        self.assertEqual(result[0]["matched_lots"][0], "SN-EXACT-001")

    def test_search_pos_products_partial_serial(self):
        result = self._search("PARTIAL")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["product_id"], self.serial_product.id)
        self.assertIn("SN-PARTIAL-123", result[0]["matched_lots"])

    def test_search_pos_products_lot_name(self):
        result = self._search("LOT-SEARCH")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["product_id"], self.lot_product.id)
        self.assertIn("LOT-SEARCH-001", result[0]["matched_lots"])

    def test_search_pos_products_wrong_pos_location(self):
        result = self._search("SN-EXACT-001", pos_config=self.alt_pos_config)
        self.assertEqual(result, [])

    def test_search_pos_products_excludes_sold_serial(self):
        result = self._search("SN-SOLD-001")
        self.assertEqual(result, [])

    def test_search_pos_products_includes_returned_serial(self):
        result = self._search("SN-RETURNED-001")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["product_id"], self.serial_product.id)

    def test_search_pos_products_excludes_zero_qty(self):
        result = self._search("SN-ZERO-001")
        self.assertEqual(result, [])

    def test_search_pos_products_duplicate_lot_names_across_products(self):
        result = self._search("DUP-LOT-001")
        self.assertEqual({item["product_id"] for item in result}, {self.duplicate_product_a.id, self.duplicate_product_b.id})

    def test_search_pos_products_company_isolation(self):
        result = self._search("SN-COMPANY-002")
        self.assertEqual(result, [])

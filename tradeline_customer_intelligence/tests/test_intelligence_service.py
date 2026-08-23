from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestIntelligenceEntityGrain(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.parent_category = cls.env["product.category"].create({"name": "Intelligence Test Parent"})
        cls.child_category = cls.env["product.category"].create(
            {"name": "Intelligence Test Child", "parent_id": cls.parent_category.id}
        )
        cls.template = cls.env["product.template"].create(
            {"name": "Intelligence Test Phone", "categ_id": cls.child_category.id}
        )
        cls.variant = cls.template.product_variant_id
        cls.variant.write({"barcode": "ab-12 3/xyz", "default_code": "fallback-code"})
        cls.service = cls.env["tradeline.customer.intelligence.service"]

    def test_category_scope_includes_descendants(self):
        entity = self.service._normalize_entity(
            {"type": "category", "id": self.parent_category.id, "name": "Ignored client label"}, ""
        )
        self.assertEqual(entity["type"], "category")
        self.assertEqual(entity["name"], self.parent_category.display_name)
        self.assertIn(self.parent_category.id, entity["category_ids"])
        self.assertIn(self.child_category.id, entity["category_ids"])
        self.assertIn("AB123", entity["prefixes"])
        clause, params = self.service._anchor_clause(entity, "", source="current", scoped=True)
        self.assertEqual(clause, "category_id = ANY(%s)")
        self.assertEqual(params, [entity["category_ids"]])

    def test_product_and_variant_are_distinct_exact_grains(self):
        product_entity = self.service._normalize_entity(
            {"type": "product", "id": self.template.id}, self.template.name
        )
        variant_entity = self.service._normalize_entity(
            {"type": "variant", "id": self.variant.id}, self.variant.display_name
        )
        product_clause, product_params = self.service._anchor_clause(
            product_entity, "", source="current", scoped=True
        )
        variant_clause, variant_params = self.service._anchor_clause(
            variant_entity, "", source="current", scoped=True
        )
        self.assertEqual((product_clause, product_params), ("product_tmpl_id = %s", [self.template.id]))
        self.assertEqual((variant_clause, variant_params), ("product_id = %s", [self.variant.id]))

    def test_legacy_exact_grains_use_normalized_prefix5(self):
        entity = self.service._normalize_entity(
            {"type": "variant", "id": self.variant.id}, self.variant.display_name
        )
        clause, params = self.service._anchor_clause(entity, "", source="legacy", scoped=False)
        self.assertIn("REGEXP_REPLACE", clause)
        self.assertIn("LEFT", clause)
        self.assertEqual(params, [["AB123"]])
        scoped_clause, scoped_params = self.service._anchor_clause(entity, "", source="legacy", scoped=True)
        self.assertIn("COALESCE(item_code, '')", scoped_clause)
        self.assertEqual(scoped_params, [["AB123"]])

    def test_prefix_prefers_item_barcode_and_normalizes(self):
        entity = self.service._normalize_entity(
            {"type": "product", "id": self.template.id}, self.template.name
        )
        self.assertEqual(entity["prefixes"], ["AB123"])
        self.assertEqual(self.service._code_prefix(" mf-ym4/af/a "), "MFYM4")

    def test_invalid_exact_entity_falls_back_to_safe_query(self):
        entity = self.service._normalize_entity(
            {"type": "variant", "id": "not-an-id", "name": "iPhone"}, "iPhone"
        )
        self.assertEqual(entity["type"], "query")
        self.assertEqual(entity["id"], 0)

    def test_search_contract_exposes_all_three_grains(self):
        results = self.service.search_entities("Intelligence Test", 12)
        result_pairs = {(item["type"], item["id"]) for item in results}
        self.assertIn(("category", self.parent_category.id), result_pairs)
        self.assertIn(("product", self.template.id), result_pairs)
        self.assertIn(("variant", self.variant.id), result_pairs)

    def test_search_accepts_human_name_or_full_item_code(self):
        results = self.service.search_entities("ab-12 3", 12)
        variant_result = next(
            item for item in results if item["type"] == "variant" and item["id"] == self.variant.id
        )
        self.assertEqual(variant_result["item_code"], "ab-12 3/xyz")
        self.assertEqual(variant_result["match_hint"], "Matched by item code")
        self.assertIn("Exact SKU", variant_result["subtitle"])

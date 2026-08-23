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
        cls.service = cls.env["tradeline.customer.intelligence.service"]

    def test_category_scope_includes_descendants(self):
        entity = self.service._normalize_entity(
            {"type": "category", "id": self.parent_category.id, "name": "Ignored client label"}, ""
        )
        self.assertEqual(entity["type"], "category")
        self.assertEqual(entity["name"], self.parent_category.display_name)
        self.assertIn(self.parent_category.id, entity["category_ids"])
        self.assertIn(self.child_category.id, entity["category_ids"])
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

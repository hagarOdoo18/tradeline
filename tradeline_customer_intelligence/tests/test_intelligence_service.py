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
        cls.legacy_fact = cls.env["legacy.product.month.fact"].create(
            {
                "source_db": "intelligence_test",
                "source_product_id": 991001,
                "period_month": "2025-01-01",
                "source_default_code": "ab-12 3-old",
                "source_name": "Legacy Intelligence Phone",
                "legacy_sales_qty": 2,
                "legacy_sales_amount": 100,
            }
        )
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

    def test_legacy_variant_keeps_prefix_without_claiming_current_record(self):
        entity = self.service._normalize_entity(
            {
                "type": "legacy_variant",
                "id": 0,
                "name": "Legacy Intelligence Phone",
                "item_code": "ab-12 3-old",
                "prefix5": "AB123",
            },
            "",
        )
        self.assertEqual(entity["type"], "legacy_variant")
        self.assertEqual(entity["id"], 0)
        self.assertEqual(entity["prefixes"], ["AB123"])
        current_clause, current_params = self.service._anchor_clause(
            entity, "", source="current", scoped=False
        )
        self.assertIn("product.barcode", current_clause)
        self.assertEqual(current_params, [["AB123"]])

    def test_variant_identity_uses_catalog_existence_not_sales_activity(self):
        entity = self.service._normalize_entity(
            {"type": "variant", "id": self.variant.id}, self.variant.display_name
        )
        identity = self.service._comparison_identity_rows(["AB123"], entity)[0]
        self.assertEqual(identity["state"], "matched")
        self.assertTrue(identity["legacy_exists"])
        self.assertTrue(identity["current_exists"])
        self.assertEqual(identity["current_variant_id"], self.variant.id)
        self.assertEqual(identity["current_catalog_status"], "Active")

    def test_query_comparison_resolves_a_bounded_prefix_scope(self):
        for index in range(6):
            self.env["legacy.product.month.fact"].create(
                {
                    "source_db": "intelligence_test",
                    "source_product_id": 992000 + index,
                    "period_month": "2025-02-01",
                    "source_default_code": f"BQ{index:03d}-legacy",
                    "source_name": f"Bounded Query Phone {index}",
                    "legacy_sales_qty": index + 1,
                }
            )
        query_entity = self.service._normalize_entity(None, "Bounded Query Phone")
        prefixes, mode = self.service._resolve_comparison_prefixes(
            query_entity, "Bounded Query Phone", limit=3
        )
        self.assertEqual(mode, "bounded_query")
        self.assertLessEqual(len(prefixes), 3)
        self.assertTrue(prefixes)
        self.assertTrue(all(len(prefix) == 5 for prefix in prefixes))

    def test_selected_variant_bypasses_free_text_prefix_resolution(self):
        entity = self.service._normalize_entity(
            {"type": "variant", "id": self.variant.id}, self.variant.display_name
        )
        prefixes, mode = self.service._resolve_comparison_prefixes(entity, "unrelated text", limit=1)
        self.assertEqual(mode, "selected_entity")
        self.assertEqual(prefixes, ["AB123"])

    def test_query_prefix_resolution_normalizes_formatted_item_codes(self):
        prefixes = self.service._comparison_query_prefixes("ab123", limit=2)
        self.assertIn("AB123", prefixes)

    def test_operating_company_filter_is_limited_to_allowed_companies(self):
        normalized = self.service._normalize_filters(
            {"operating_company_id": self.env.company.id}
        )
        self.assertEqual(normalized["operating_company_id"], self.env.company.id)
        self.assertEqual(self.service._company_ids(normalized), [self.env.company.id])
        invalid = self.service._normalize_filters({"operating_company_id": 999999999})
        self.assertEqual(invalid["operating_company_id"], 0)
        self.assertEqual(set(self.service._company_ids(invalid)), set(self.env.user.company_ids.ids))

    def test_search_contract_exposes_all_three_grains(self):
        results = self.service.search_entities("Intelligence Test", 12)
        result_pairs = {(item["type"], item["id"]) for item in results}
        self.assertIn(("category", self.parent_category.id), result_pairs)
        self.assertIn(("product", self.template.id), result_pairs)
        self.assertIn(("variant", self.variant.id), result_pairs)

    def test_search_exposes_legacy_monthly_variant_without_fake_odoo18_id(self):
        results = self.service.search_entities("Legacy Intelligence Phone", 12)
        legacy = next(item for item in results if item["type"] == "legacy_variant")
        self.assertEqual(legacy["id"], 0)
        self.assertEqual(legacy["prefix5"], "AB123")
        self.assertEqual(legacy["source"], "legacy")

    def test_search_accepts_human_name_or_full_item_code(self):
        results = self.service.search_entities("ab-12 3", 12)
        variant_result = next(
            item for item in results if item["type"] == "variant" and item["id"] == self.variant.id
        )
        self.assertEqual(variant_result["item_code"], "ab-12 3/xyz")
        self.assertEqual(variant_result["match_hint"], "Matched by item code")
        self.assertIn(self.child_category.display_name, variant_result["subtitle"])
        self.assertNotIn("Exact SKU · Exact SKU", variant_result["subtitle"])

    def test_customer_population_filter_is_sql_scoped(self):
        company = self.env.company.partner_id
        normalized = self.service._normalize_filters(
            {"customer_type": "company", "customer_company_id": company.id}
        )
        clause, params = self.service._audience_sql("move", normalized)
        self.assertIn("commercial.is_company", clause)
        self.assertIn("commercial.id = %s", clause)
        self.assertEqual(params, [company.id])

        legacy_clause, legacy_params = self.service._audience_sql("invoice", normalized, source="legacy")
        self.assertIn("invoice.source_partner_type", legacy_clause)
        self.assertIn("invoice.source_partner_name", legacy_clause)
        self.assertIn(company.name.strip().lower(), legacy_params)

        all_clause, all_params = self.service._audience_sql("move", {"customer_type": "all"})
        self.assertEqual(all_clause, "")
        self.assertEqual(all_params, [])

    def test_query_evidence_uses_the_same_searchable_product_fields(self):
        query_entity = self.service._normalize_entity(None, "iPhone 17")

        legacy_domain = self.service._evidence_anchor_domain(query_entity, "iPhone 17", "legacy")
        self.assertIn(("line_ids.product_name", "ilike", "iPhone 17"), legacy_domain)
        self.assertIn(("line_ids.item_code", "ilike", "iPhone 17"), legacy_domain)
        self.assertNotIn(("line_ids.product_search_text", "ilike", "iPhone 17"), legacy_domain)

        current_domain = self.service._evidence_anchor_domain(query_entity, "iPhone 17", "current")
        self.assertIn(
            ("invoice_line_ids.product_id.product_tmpl_id.name", "ilike", "iPhone 17"),
            current_domain,
        )
        self.assertIn(("invoice_line_ids.product_id.default_code", "ilike", "iPhone 17"), current_domain)

    def test_evidence_action_declares_odoo18_list_and_form_views(self):
        action = self.service.open_evidence(
            self.template.name,
            source="current",
            entity={"type": "variant", "id": self.variant.id},
        )
        self.assertEqual(action["res_model"], "account.move")
        self.assertEqual(action["views"], [(False, "list"), (False, "form")])

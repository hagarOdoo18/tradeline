from datetime import date

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
        legacy_clause, legacy_params = self.service._anchor_clause(
            entity, "", source="legacy", scoped=False
        )
        self.assertIn("line.product_category_id = ANY(%s)", legacy_clause)
        self.assertIn("REGEXP_REPLACE", legacy_clause)
        self.assertEqual(legacy_params, [entity["category_ids"], entity["prefixes"]])

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
        self.assertEqual(self.service._code_prefix("False"), "")

    def test_transport_safe_replaces_nested_nulls_for_xmlrpc(self):
        self.assertEqual(
            self.service._transport_safe({"value": None, "rows": [1, None]}),
            {"value": False, "rows": [1, False]},
        )

    def test_business_source_labels_hide_technical_versions(self):
        coverage = self.service._coverage(
            {"current": 2, "legacy": 3}, "legacy", date(2025, 9, 1), date(2025, 12, 31)
        )
        self.assertEqual(
            [source["label"] for source in coverage["sources"][:2]],
            ["Current operations", "Historical sales"],
        )
        self.assertNotIn("Odoo", coverage["rule"])
        self.assertIn("authoritative ledger", coverage["rule"])

    def test_legacy_prefix_ignores_false_sentinel_and_uses_barcode(self):
        sentinel_fact = self.env["legacy.product.month.fact"].create(
            {
                "source_db": "intelligence_test",
                "source_product_id": 991002,
                "period_month": "2025-01-01",
                "source_default_code": "False",
                "source_barcode": "MG6J4AF/A",
                "source_name": "Legacy Sentinel Phone",
                "legacy_sales_qty": 3,
                "legacy_sales_amount": 250,
            }
        )
        self.assertTrue(sentinel_fact)
        prefixes = self.service._comparison_query_prefixes("Legacy Sentinel Phone", limit=2)
        self.assertIn("MG6J4", prefixes)
        self.assertNotIn("FALSE", prefixes)
        january = self.service._comparison_legacy_metric_rows(["MG6J4"])[0]
        self.assertEqual(january["sales_qty"], 3)
        self.assertEqual(january["observed_prefixes"], ["MG6J4"])

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

    def test_selected_variant_keeps_exact_current_activity_product(self):
        sibling_template = self.env["product.template"].create(
            {"name": "Same-prefix sibling", "categ_id": self.child_category.id}
        )
        sibling = sibling_template.product_variant_id
        sibling.barcode = "AB123-sibling"
        entity = self.service._normalize_entity(
            {"type": "variant", "id": self.variant.id}, self.variant.display_name
        )
        product_ids = self.service._comparison_current_product_ids(["AB123"], entity)
        self.assertEqual(product_ids, [self.variant.id])
        self.assertNotIn(sibling.id, product_ids)

    def test_legacy_comparison_metrics_read_physical_month_fact(self):
        rows = self.service._comparison_legacy_metric_rows(["AB123"])
        january = next(row for row in rows if row["month_number"] == 1)
        self.assertEqual(january["source_system"], "legacy")
        self.assertEqual(january["sales_qty"], 2)
        self.assertEqual(january["sales_amount"], 100)
        self.assertEqual(january["observed_prefixes"], ["AB123"])

    def test_current_comparison_uses_posted_invoice_lines_after_product_resolution(self):
        entity = self.service._normalize_entity(
            {"type": "variant", "id": self.variant.id}, self.variant.display_name
        )
        rows, metadata = self.service._comparison_current_metric_rows(
            ["AB123"], entity, {"operating_company_id": self.env.company.id}
        )
        self.assertEqual(rows, [])
        self.assertEqual(metadata["source"], "account.move.line")
        self.assertEqual(metadata["product_count"], 1)
        self.assertTrue(metadata["available"])

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

    def test_legacy_business_scope_uses_source_markers_not_import_owner(self):
        xprs = self.env["res.company"].create({"name": "XPRS Intelligence Test"})
        self.env.user.company_ids |= xprs
        xprs_sql, xprs_params = self.service._legacy_business_sql(
            "invoice", {"operating_company_id": xprs.id}
        )
        tradeline_sql, tradeline_params = self.service._legacy_business_sql(
            "invoice", {"operating_company_id": self.env.company.id}
        )
        self.assertIn("source_journal_name", xprs_sql)
        self.assertNotIn("NOT (", xprs_sql)
        self.assertEqual(xprs_params, ["%xprs%", "%-x/%"] * 6)
        if "tradeline" in self.env.company.name.lower():
            self.assertIn("AND NOT", tradeline_sql)
            self.assertEqual(tradeline_params, ["%xprs%", "%-x/%"] * 6)

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

    def test_catalog_brand_is_human_readable_and_not_the_identity_key(self):
        self.assertEqual(self.service._catalog_brand("Apple iPhone 17 256GB Black"), "Apple")
        self.assertEqual(self.service._catalog_brand("[SKU] iPhone 16 Pro"), "Apple")
        self.assertEqual(self.service._catalog_brand("Belkin BoostCharge 45W"), "Belkin")
        self.assertEqual(self.service._catalog_brand("23% Launch Discount"), "Other")
        self.assertEqual(self.service._catalog_brand("Assassin's Creed PS5"), "Other")

    def test_sparse_scope_probability_is_bounded(self):
        self.assertEqual(self.service._bounded_probability(0, 0), 0.0)
        self.assertEqual(self.service._bounded_probability(3, 10), 0.3)
        self.assertEqual(self.service._bounded_probability(12, 10), 1.0)
        self.assertEqual(self.service._bounded_probability(-1, 10), 0.0)

    def test_guided_catalog_resolves_exact_variant_and_hidden_prefix(self):
        catalog = self.service.get_catalog_options({"variant_id": self.variant.id})
        self.assertEqual(catalog["selection"]["variant_id"], self.variant.id)
        self.assertEqual(catalog["selection"]["product_id"], self.template.id)
        selected = catalog["selected_variant"]
        self.assertEqual(selected["type"], "variant")
        self.assertEqual(selected["prefix5"], "AB123")
        self.assertEqual(selected["coverage_label"], "Historical + current")
        self.assertEqual(catalog["join_rule"], "normalized_item_prefix_5")

    def test_dimension_merge_does_not_double_first_source(self):
        merged = self.service._merge_dimension_rows(
            [
                [{"period": "2025-01", "label": "Jan 2025", "baskets": 2, "revenue": 50}],
                [{"period": "2025-01", "label": "Jan 2025", "baskets": 3, "revenue": 75}],
            ],
            value_fields=("baskets", "revenue"),
        )
        self.assertEqual(merged[0]["baskets"], 5)
        self.assertEqual(merged[0]["revenue"], 125)

    def test_unified_merge_keeps_dimension_based_identified_baskets(self):
        current = (
            [{"product_key": "c", "product_name": "Case", "prefix5": "CASE1", "co_baskets": 2,
              "base_baskets": 4, "anchor_baskets": 4, "companion_baskets": 2,
              "all_baskets": 10, "identified_baskets": 1, "identified_customers": 1}],
            [], [],
            {"scope_summary": {"baskets": 4, "identified_baskets": 3, "identified_customers": 2}},
        )
        legacy = (
            [{"product_key": "l", "product_name": "Case", "prefix5": "CASE1", "co_baskets": 3,
              "base_baskets": 5, "anchor_baskets": 5, "companion_baskets": 3,
              "all_baskets": 12, "identified_baskets": 0, "identified_customers": 0}],
            [], [],
            {"scope_summary": {"baskets": 5, "identified_baskets": 5, "identified_customers": 3}},
        )
        _companions, _customers, _payments, dimensions = self.service._merge_source_results(
            current, legacy, 20
        )
        self.assertEqual(dimensions["scope_summary"]["baskets"], 9)
        self.assertEqual(dimensions["scope_summary"]["identified_baskets"], 8)

    def test_identity_summary_discloses_ambiguous_legacy_prefix_family(self):
        self.env["legacy.product.month.fact"].create(
            {
                "source_db": "intelligence_test",
                "source_product_id": 991003,
                "period_month": "2025-02-01",
                "source_default_code": "AB123-second",
                "source_name": "Second Legacy Intelligence Phone",
                "legacy_sales_qty": 1,
            }
        )
        entity = self.service._normalize_entity(
            {"type": "variant", "id": self.variant.id}, self.variant.display_name
        )
        summary = self.service._product_identity_summary(entity)
        self.assertEqual(summary["identity_precision"], "prefix_family")
        self.assertEqual(summary["legacy_variant_count"], 2)
        self.assertIn("historical prefix family", summary["identity_label"].lower())

    def test_iphone_generation_drives_upgrade_gap_without_ml_claim(self):
        self.assertEqual(self.service._iphone_generation("Apple iPhone 17 256GB Black"), 17)
        self.assertEqual(self.service._iphone_generation("Apple iPhone SE 64GB"), 0)

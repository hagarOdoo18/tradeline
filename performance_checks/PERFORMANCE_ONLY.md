# Performance-only staging candidate

The active change is limited to:
- POS lot loading: aggregate stock quantities in batches while retaining the complete catalogue and native access rules.
- Invoice Excel export: reuse metadata only during the current export and stream worksheet XML.
- Apple stock Excel export: reuse the selected stock quants and stream worksheet XML.
- Shopify log access: exact grouped SQL counts, a compact instance filter for the log action, one shared counter compute, and an index on the log's Shopify instance. No log deletion or retention changes.

There are no new request quotas, transaction caps, scan cooldowns, row caps, or changes to stock posting/import code. The 10,000-lot batch size is an internal query batch size, not a catalogue limit. The 1,000-entry invoice cache is cleared and reused; it does not limit invoices or rows.

The active POS scanning JavaScript remains unchanged from the stage base:
- product_screen.js: bd7175eac4c978ed53ede6aff0752f276cf79f9e
- navbar.js: 541fe0e559e9bb08c37a9423fa0aafa007e38ba8

The duplicate-across-open-carts guard, immediate Enter adjustment, and cached offline serial lookup are retained only in held_scanning_offline/scanning-offline.patch. This patch is not loaded by Odoo, not listed in assets, and must not be applied during this performance-only release. Its original combined candidate is also retained at tag archive/codex-cleanup-20261022/local/pos-excel-performance.

## Staging validation

Use performance_checks/run_staff_regressions_staging.py with odoo-bin shell on the neutralized staging database. It exercises existing POS loading/search/serial/refund/configurator rules, the real stock import and multi-product Excel posting suites, and Shopify log access/counter regressions. TL_QA_OUTPUT_DIR selects a separate evidence directory; TL_QA_MODULES optionally selects named suites for a targeted recheck. Large explicit stress suites are excluded; ordinary correctness suites are included. Requests to external HTTP services are refused. Tests run in Odoo transaction cases, and the runner rolls back its cursor.

Use performance_checks/check_pos_excel_staging.py with TL_FULL_LOTS=1 for complete lot-payload and decoded Excel cell/style comparisons against the saved original methods. The report role variants compare output columns; they are not a complete report-user permission matrix.

The running stage database was restored from build fd78d164 and includes other agents' uncommitted legacy-customer and manifest edits. Preserve these overlays. Do not recreate staging, upgrade modules, or replace unrelated sources to deploy this isolated change.

For this restored staging DB, performance_checks/apply_shopify_log_index_staging.py creates the ORM-declared index concurrently without a module upgrade. It refuses other databases. Its SQL lock/statement timeouts apply only to that maintenance session. They do not limit staff requests or imports. performance_checks/benchmark_shopify_staging.py checks read plans, exact counts and complete log paging without expanding the One2many.

The user authorized the isolated production performance release on 23 October 2026; see PRODUCTION_RELEASE_20261023.md. Physical scanner hardware and a complete offline acceptance session have not been validated; this change does not introduce the held offline feature. The inactive patch remains preserved on stage and in the archive tag.

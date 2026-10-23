# Performance production release — 23 October 2026

The user explicitly authorized production deployment after staging validation.
The release is built on production commit `981a58888e9cf2afedfdb81e81c638ea61102ecb`, preserving newer production work, and carries the five tested runtime files from stage `47b992e572eec23b9d4cab4b0db9b6fe76341845`.

Runtime scope:
- POS lot loading: grouped quantity reads with the complete catalogue and native rules.
- Invoice and Apple stock Excel reports: request-local metadata reuse and streamed worksheet XML.
- Shopify configuration counters/log action: exact counts without expanding millions of IDs, plus a non-unique index on the log's instance.

No scanning/offline JavaScript, stock import/posting implementation, payment implementation, Shopify synchronization logic, business timeouts, request quotas, data limits, or unrelated production modules are changed. No addon manifest version is bumped and no broad module upgrade is requested.

Staging evidence: 65 passing regression cases (POS loading/serial/location/refund/configurator, stock import/accounting/retries/permissions, multi-stock posting and Shopify counters/access). All 66 measured list/kanban reads completed without errors. Complete earlier lot payload and Excel workbook comparisons are recorded in `VALIDATION_20261023.md` and `SHOPIFY_QUERY_VALIDATION_20261023.md`. These checks reduce risk; they do not guarantee every staff workflow under concurrent production load.

Before the push, create and verify a fresh Odoo.sh production backup. Verify the current runtime files match the production Git base; the existing manifest permission-only differences contain no source edits. Install the tested log index concurrently using `apply_shopify_log_index_production.py`, which refuses every other database. This additive index remains compatible with the previous application code. If the index build fails, inspect validity and stop before deployment.

Deploy by fast-forwarding `production` with this isolated release, retaining the standard Odoo.sh backup/update process. Never merge the older stage tree over production. Verify the final build is successful, its live source hashes match this commit, the original scanning hashes remain unchanged, and the read-only post-release checks pass. Do not run the transaction regression suites or create QA POS sales in production.

Rollback: retain base commit `981a58888e9cf2afedfdb81e81c638ea61102ecb` and revert this release commit through a new commit if an application regression is found. The additive index can stay during a code rollback. A database restore would discard transactions entered after the backup and is not the default rollback for this code-only change.

Live release evidence is written under `~/data/tl-performance-release-20261023/`; local evidence is under `C:/Users/karim/.codex/tmp/odoo-production-performance-20261023/`.

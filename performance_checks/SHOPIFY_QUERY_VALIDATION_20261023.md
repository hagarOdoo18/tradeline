# Shopify log query optimization — staging, 23 October 2026

Database: `tradelinestores-stage-40209081`, neutralized for testing.
Branch base: `1f7c5c455ec38ddf4883b594a6a9ceea5332c2a9`.
Running restored build: `fd78d164c6aea670d7c5594f7cb0599f7755a2c2`, with preserved source overlays.
Production remains on `981a58888e9cf2afedfdb81e81c638ea61102ecb` and is outside this deployment.

## Changes

- Replace `len(log_message_ids)` for persisted instance counters with one exact ORM grouped count. Unsaved forms retain their in-memory log count.
- Open logs with `shopify_instance_id = current instance` rather than serializing every matching ID. Preserve the original empty-action behavior, context, ordering, ACLs and record rules. The existence probe uses one row; the returned action has no new row limit.
- Give all six counters the same compute method so reading gift cards does not trigger a second full computation. Retain the old private gift-card compute entry point for compatibility.
- Declare a non-unique B-tree index on `log.message.shopify_instance_id`. Install that index concurrently on this restored staging DB without upgrading modules or rebuilding the environment.

No log deletion, retention policy, business request limit, new import batch size, scan delay, transaction cap, default date filter, or staff permission change was introduced. Stock import, stock posting, payment, Shopify synchronization and report implementation code were not changed. The earlier POS/Excel optimization stays active and the proposed scanning/offline patch stays inactive.

## Measurements

The instance has **3,734,295 logs**. The previous count plan scanned 344,256 table blocks (about 2.63 GiB) and took 683.245 ms. The indexed plan used an index-only scan, touched 3,284 shared blocks, fetched 614 heap tuples and took 125.300 ms. The index occupies 25,903,104 bytes (24.7 MiB).

After optimization:

| Operation | Staging sample |
|---|---:|
| All six Shopify counters | 97.36 ms |
| Build the instance log action | 0.338 ms |
| Serialized log action | 234 bytes |
| Database query for first 80 log IDs | 0.046 ms |
| Exact log count | 3,734,295 |

Both first and second native log pages returned 80 distinct records. Instrumentation rejected any attempt to expand the persisted log One2many during the measured counter/action reads. These are backend samples, with differing cache states, not end-to-end browser guarantees or a concurrent production benchmark. The old millions-of-IDs action was deliberately not replayed against the full staging dataset.

## Regression results

| Suite | Passed | Failures/errors |
|---|---:|---:|
| POS loading | 6 | 0 |
| Existing POS serial, location, refund and configurator rules | 22 | 0 |
| Stock Excel imports | 18 | 0 |
| Multi-product stock Excel posting | 11 | 0 |
| Shopify log access and counters | 8 | 0 |
| **Total** | **65** | **0** |

Stock coverage includes native stock valuation/accounting entries, repeated products, serial imports, existing-location checks, background completion/retries, rollback after a failed batch, and the importing staff user's permissions. Explicit large stress suites remain excluded. External HTTP is blocked during these tests; business test writes are rolled back.

Shopify coverage includes exact multiple-instance counts, empty instances, complete action domains without row caps, access rules for an internal staff user, all five other counters with linked records, an unsaved form, compatibility with the old compute entry point and newly added logs. Initial test fixtures omitted this installation's required branch/sales representative; fixtures were corrected and the Shopify suite rerun successfully. The 57 existing workflow cases passed both full runs; the final eight Shopify cases passed in a separate fresh shell. Final results are combined by module in `staff-verified-results.json`.

The post-change back-office audit considered 85 window actions: 66 list/kanban reads, including 55 populated results, and no errors. Median populated first-page backend read was 0.093 seconds; 54 of 55 were below 0.5 seconds. Six additional transfer/move/POS/report workflow reads completed without errors. This uses native paging and representative form specifications, not every nested field or every staff role.

## Remaining slow paths

Sales Analysis still took 1.78 seconds for its first page because its Sales/POS union aggregates approximately 251,000 historical groups before sorting. A measured two-step read returned identical values but took 2.36 seconds versus 1.90 seconds for the native path, so it was not deployed. No default history/date filter or core report override was added.

Purchase order and Contact forms still include native computed metrics (about 1.55 and 0.72 seconds respectively in the profiled audit). This change targets Shopify log overhead; it does not make every Odoo window faster. These findings do not establish that purchasing more host RAM is necessary.

## Preservation and release hold

Before applying source changes, all staging tracked/staged diffs and untracked files were archived under `~/data/tl-shopify-query-20261023/` (`working-before-082834.patch`, `index-before-082834.patch`, `untracked-before-082834.tar.gz`, and `source-before-082834/`). Other agents' legacy-customer and manifest overlays were preserved. Only staging's HTTP worker was reloaded; no module upgrade, database restore, cron change or production deployment was performed.

Original scanning JavaScript hashes remain:
- `product_screen.js`: `bd7175eac4c978ed53ede6aff0752f276cf79f9e`
- `navbar.js`: `541fe0e559e9bb08c37a9423fa0aafa007e38ba8`

Hosted evidence: `~/data/tl-shopify-query-20261023/shopify-qa-evidence.json`, `diagnosis.json`, `staff.log`, `shopify-recheck/staff.log`, `benchmark.json`, `windows/windows-results.json`, `windows/workflow-results.json`, `index-install.log`, `http-restart.log`.

Local evidence: `C:/Users/karim/.codex/tmp/shopify-query-20261023/`.

If rollback is needed, restore only these Shopify source files from their saved copies and reload staging HTTP; do not reset the restored checkout. The additive index can remain, or be removed concurrently in a separately guarded staging maintenance session. Production release and any production index installation require a separate user instruction.

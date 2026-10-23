# Performance-only staging validation — 23 October 2026

Staging database: tradelinestores-stage-40209081.
Running restored build base: fd78d164c6aea670d7c5594f7cb0599f7755a2c2, with preserved source overlays.
Stage branch base: d3a668efadefee41738ff2186b86147cfd69afb0.
Active performance commit: 08d4108.
Inactive scanning/offline archive commit: 1a7293f.

## Results

| Check | Result |
|---|---|
| POS loading transaction cases | 6 passed, 0 failures/errors |
| Existing POS serial/search/refund/configurator transaction cases | 22 passed, 0 failures/errors |
| Stock Excel import correctness cases | 18 passed, 0 failures/errors |
| Multi-product stock Excel posting cases | 11 passed, 0 failures/errors |
| Complete lot catalogue comparison | 200,332 rows matched native fields, order and quantities within 1e-8 |
| Invoice Excel comparison | All four column-role variants matched decoded cell values, formulas, number formats and styles; 2,592 worksheet rows each |
| Stock Excel comparison | Both vendor-column variants matched decoded cell values, formulas, number formats and styles; 24,899 worksheet rows each |
| Browser QA register | Loaded; product search, adding plain stock, changing quantity, exact online serial entry, and opening/backing out of payment screen worked |
| Browser QA cart | Order 103: 2 plain units plus serial 9900026102111; total 20 LE; left unpaid for review |

All 57 transaction cases passed. The runner reported STAFF_QA_WRITES_ROLLED_BACK. Golden comparison runner reported POS_EXCEL_ALL_COMPARISONS_PASSED and ALL_COMPARISON_WRITES_ROLLED_BACK. No paid POS order was validated or submitted. CLI tests blocked external HTTP requests. Explicit large stress suites were excluded from this correctness run.

The synthetic QA stock fixture adds two lots compared with the earlier pre-fixture catalogue comparison. Both expected and optimized payloads used the same current 200,332 rows.

## Separation and preservation

Active production JavaScript is unchanged from the stage base and matches the pre-experiment hashes recorded in PERFORMANCE_ONLY.md. The new cross-cart duplicate guard, immediate Enter adjustment and cached offline fallback are present only as an inactive patch. Stock import, stock posting, serial-upload and payment implementation source files were not changed.

Before restoring the two experimental JS files, all staging tracked edits, staged edits and untracked files were saved under ~/data/tl-pos-excel-separated-20261023. Existing legacy-customer and manifest overlays were left untouched. Eight regenerable POS asset bundles were invalidated on staging so the browser could load the original scanning sources; business records were not removed.

Hosted raw evidence:
- ~/data/tl-pos-excel-separated-20261023/status.txt
- ~/data/tl-pos-excel-separated-20261023/staff.log
- ~/data/tl-pos-excel-separated-20261023/staff-results.json
- ~/data/tl-pos-excel-separated-20261023/comparison.log
- ~/data/tl-pos-excel-separated-20261023/activate.log
- ~/data/tl-pos-excel-separated-20261023/separation-source.json

Local browser screenshot:
C:/Users/karim/.codex/tmp/pos-excel-separation-20261023/pos-normal-cart.png

## Limits and release hold

This is representative regression coverage, not proof that every workflow for every staff role is unaffected. The invoice output variants do not constitute a complete user/permission matrix. A physical barcode scanner was not tested. Fully disconnected hardware testing remains outstanding and no new offline functionality is active. The browser test used Administrator in the synthetic register.

Only stage is authorized. Do not merge or push these commits to production without a separate approval. Production references and runtime configuration were not changed by this task.

# Temporary SMTP retries and Operations Monitor error details

Based on production mail 14109, which failed with Google's explicit SMTP
451 / 4.3.0 temporary rejection and was successfully retried manually.

## Validation

- Local Odoo 18 Community with PostgreSQL 18.6, an isolated test database,
  and mocked SMTP transport. No production email was sent by these tests.
- Final fresh-install run: **34 test methods, 0 failures, 0 errors**.
  Three pre-existing optional connector/import tests skipped because those
  unrelated modules are not installed in the test database.
- Twelve new regression tests exercise the real Odoo mail send/postprocess
  flow, 451/421 rejection, authentication and permanent failure handling,
  backoff/cap, repeat queue reservation, partial and mixed recipient sends,
  cancellation, historical-failure exclusion, successful notification cleanup,
  error snapshots and exclusion from alert-email/health responses.
- Module upgrade exercised against the first test installation.
- Browser check: actual SMTP error, retry count, next scheduled retry and
  original-email link rendered correctly; no browser errors or warnings.
- `git diff --check` and Python compilation passed.

The preview screenshot uses a synthetic email in the isolated local database.

## Rollout

Upgrade `tradeline_operations_monitor` to 18.0.1.1.0 after merging. The new
retry cron is active and runs each minute, independently of notification
settings. Due retries trigger the standard Odoo mail queue. No old failed
emails are enrolled automatically. Production has not been changed by this
branch. Mail incidents retain the existing manual-review requirement.
